# -*- coding: utf-8 -*-
"""Смещает все замкнутые контуры выбранной области поверхности."""

import math
import re

from Autodesk.Revit.DB.Architecture import SiteSubRegion, TopographySurface
from Autodesk.Revit.Exceptions import OperationCanceledException
from Autodesk.Revit.UI.Selection import ISelectionFilter, ObjectType
from System.Collections.Generic import List
from pyrevit import DB, forms, revit, script


TITLE = u"Сместить контур"
NORMAL = DB.XYZ.BasisZ
AREA_TOLERANCE = 1e-9  # square feet


class OffsetWindow(forms.WPFWindow):
    def __init__(self):
        forms.WPFWindow.__init__(self, script.get_bundle_file("Offset.xaml"))
        self.distance_mm = None
        self.outward = True
        self.Distance.Focus()
        self.Distance.SelectAll()

    def apply_offset(self, sender, args):
        value = self.Distance.Text.strip()
        if re.match(r"^[0-9]+(?:[.,][0-9]+)?$", value):
            distance = float(value.replace(",", "."))
            if distance > 0 and not math.isinf(distance):
                self.distance_mm = distance
                self.outward = bool(self.Outward.IsChecked)
                self.DialogResult = True
                return
        self.InputError.Text = u"Введите положительное расстояние в миллиметрах."
        self.Distance.Focus()
        self.Distance.SelectAll()


def is_subregion(element):
    return isinstance(element, TopographySurface) and element.IsSiteSubRegion


class SubregionFilter(ISelectionFilter):
    def AllowElement(self, element):
        return is_subregion(element)

    def AllowReference(self, reference, position):
        return False


def get_surface(uidoc):
    selected = list(uidoc.Selection.GetElementIds())
    if len(selected) == 1:
        element = uidoc.Document.GetElement(selected[0])
        if is_subregion(element):
            return element

    picked = uidoc.Selection.PickObject(
        ObjectType.Element, SubregionFilter(),
        u"Выберите область поверхности топографии. Esc — отмена."
    )
    return uidoc.Document.GetElement(picked.ElementId)


def split_single_curve(loop):
    # Revit's extrusion validator needs at least two curves in a closed loop.
    curves = list(loop)
    if len(curves) != 1 or not curves[0].IsCyclic:
        return loop

    curve = curves[0]
    start = curve.GetEndParameter(0) if curve.IsBound else 0.0
    end = curve.GetEndParameter(1) if curve.IsBound else curve.Period
    middle = (start + end) / 2
    halves = List[DB.Curve]()
    for lower, upper in ((start, middle), (middle, end)):
        half = curve.Clone()
        half.MakeBound(lower, upper)
        halves.Add(half)
    return DB.CurveLoop.Create(halves)


def describe_loops(loops):
    """Return exact planar caps and the indices of the loops containing each loop."""
    caps = []
    boundary_points = []
    solids = []
    for loop in loops:
        profile = List[DB.CurveLoop]()
        profile.Add(loop)
        solid = DB.GeometryCreationUtilities.CreateExtrusionGeometry(
            profile, NORMAL, 1.0
        )
        solids.append(solid)
        cap = next(face for face in solid.Faces
                   if isinstance(face, DB.PlanarFace)
                   and face.FaceNormal.DotProduct(NORMAL) > 0.999999)
        caps.append(cap)
        boundary_points.append(next(iter(loop)).GetEndPoint(0))

    containers = [
        tuple(index for index, cap in enumerate(caps)
              if index != own and cap.Project(point) is not None)
        for own, point in enumerate(boundary_points)
    ]
    return [cap.Area for cap in caps], containers


def offset_boundary(boundary, distance, outward):
    source = List[DB.CurveLoop]()
    for loop in boundary:
        source.Add(split_single_curve(loop))
    if not SiteSubRegion.IsValidBoundary(source):
        raise ValueError(u"Исходная граница области недопустима.")

    areas, containers = describe_loops(source)
    shifted = List[DB.CurveLoop]()
    for index, loop in enumerate(source):
        # Increasing the filled area expands outer rings and shrinks holes.
        grow = outward != (len(containers[index]) % 2 == 1)
        counterclockwise = loop.IsCounterclockwise(NORMAL)
        signed_distance = distance if grow == counterclockwise else -distance
        try:
            result = DB.CurveLoop.CreateViaOffset(loop, signed_distance, NORMAL)
        except Exception as error:
            raise ValueError(u"Revit не смог сместить контур №{}."
                             u" Уменьшите расстояние.\n{}".format(index + 1, error))
        if result.IsCounterclockwise(NORMAL) != counterclockwise:
            raise ValueError(u"Контур №{} вырождается при смещении.".format(index + 1))
        shifted.Add(split_single_curve(result))

    if not SiteSubRegion.IsValidBoundary(shifted):
        raise ValueError(u"После смещения контуры пересекаются или имеют разрыв."
                         u" Уменьшите расстояние.")

    new_areas, new_containers = describe_loops(shifted)
    for index, (before, after) in enumerate(zip(areas, new_areas)):
        grow = outward != (len(containers[index]) % 2 == 1)
        expected_change = after - before if grow else before - after
        if expected_change <= AREA_TOLERANCE or containers[index] != new_containers[index]:
            raise ValueError(u"Контур №{} вырождается или меняет вложенность."
                             u" Уменьшите расстояние.".format(index + 1))
    return shifted


def apply_boundary(doc, region, boundary):
    transaction = DB.Transaction(doc, TITLE)
    try:
        if transaction.Start() != DB.TransactionStatus.Started:
            raise ValueError(u"Revit не начал транзакцию.")
        options = transaction.GetFailureHandlingOptions()
        options.SetForcedModalHandling(True)
        transaction.SetFailureHandlingOptions(options)
        region.SetBoundary(boundary)
        if transaction.Commit() != DB.TransactionStatus.Committed:
            raise ValueError(u"Revit не принял новую границу.")
    finally:
        if transaction.GetStatus() == DB.TransactionStatus.Started:
            transaction.RollBack()
        transaction.Dispose()


def main():
    uidoc = revit.uidoc
    if uidoc is None or uidoc.Document.IsFamilyDocument:
        forms.alert(u"Откройте проект Revit.", title=TITLE)
        return

    try:
        surface = get_surface(uidoc)
        if surface.Pinned:
            forms.alert(u"Снимите закрепление выбранной области поверхности.", title=TITLE)
            return

        window = OffsetWindow()
        if not window.show_dialog():
            return

        distance = DB.UnitUtils.ConvertToInternalUnits(
            window.distance_mm, DB.UnitTypeId.Millimeters
        )
        region = surface.AsSiteSubRegion()
        try:
            boundary = offset_boundary(region.GetBoundary(), distance, window.outward)
            apply_boundary(uidoc.Document, region, boundary)
        finally:
            region.Dispose()
    except OperationCanceledException:
        return
    except Exception as error:
        forms.alert(u"Не удалось сместить границу.\n\n{}".format(error), title=TITLE)


if __name__ == "__main__":
    main()
