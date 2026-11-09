# -*- coding: utf-8 -*-
"""Creates independent landscape subregions from a selected layer of a placed DWG."""

import os
import tempfile

from Autodesk.Revit.DB.Architecture import SiteSubRegion, TopographySurface
from Autodesk.Revit.Exceptions import ArgumentException, InvalidOperationException, OperationCanceledException
from Autodesk.Revit.UI.Selection import ISelectionFilter, ObjectType
from System import Guid
from System.Collections.Generic import List
from pyrevit import DB, forms, revit, script

import landscape_geometry as geometry


TITLE = u"Области из DWG"
NAME = u"Имя"
AREA = u"Площадь озеленения"
IFC = "IfcExportAs"
IFC_ALIASES = (u"IFC-класс", IFC, "Export to IFC As", u"Экспорт в IFC как")
PARAMETERS = {
    NAME: (DB.ParameterType.Text, "5dc524c5-9e14-455a-af09-f5f3f3b57c16"),
    IFC: (DB.ParameterType.Text, "74920291-a15f-4a92-b380-41d52180cb55"),
    AREA: (DB.ParameterType.Area, "3a7c4425-e52a-4a68-946a-669895242c51"),
}


class SettingsWindow(forms.WPFWindow):
    def __init__(self, layers):
        forms.WPFWindow.__init__(self, script.get_bundle_file("Settings.xaml"))
        self.layers = sorted(layers, key=lambda name: name.lower())
        self.layer_items = layers
        self.fill_counts = [sum(geometry.is_fill(item) for item, _ in layers[name])
                            for name in self.layers]
        self.Layer.ItemsSource = [
            u"{}  ·  заливок: {}, линий: {}".format(
                name, count, sum(isinstance(item, (DB.Curve, DB.PolyLine))
                                 for item, _ in layers[name]))
            if layers[name] else u"{}  ·  нет доступных контуров или заливок".format(name)
            for name, count in zip(self.layers, self.fill_counts)
        ]
        if len(self.layers) == 1:
            self.Layer.SelectedIndex = 0

    def layer_changed(self, sender, args):
        index = self.Layer.SelectedIndex
        if index >= 0:
            self.InputError.Text = ""
            self.Source.SelectedIndex = 0 if self.fill_counts[index] else 1

    def accept(self, sender, args):
        index = self.Layer.SelectedIndex
        if index < 0:
            typed = self.Layer.Text.strip().lower()
            matches = [i for i, name in enumerate(self.layers) if name.lower() == typed]
            if len(matches) != 1:
                self.InputError.Text = u"Выберите слой из списка или введите его точное имя."
                return
            self.Layer.SelectedIndex = matches[0]
            index = matches[0]
        name = self.layers[index]
        if not self.layer_items[name]:
            self.InputError.Text = (
                u"Слой есть в DWG, но Revit не передал его линии, грани или сетки. "
                u"Если это штриховка, создайте её границы командой HATCHGENERATEBOUNDARY "
                u"в AutoCAD, вынесите полилинии на отдельный слой и обновите DWG."
            )
            return
        self.layer_name = name
        self.read_fills = self.Source.SelectedIndex == 0
        self.region_name = self.RegionName.Text.strip()
        self.ifc_class = self.IfcClass.Text.strip()
        self.material = self.MaterialName.Text.strip()
        self.write_area = bool(self.WriteArea.IsChecked)
        self.DialogResult = True


def is_host(element):
    return (isinstance(element, TopographySurface)
            and not element.IsSiteSubRegion
            and not element.IsAssociatedWithBuildingPad)


class CadFilter(ISelectionFilter):
    def AllowElement(self, element):
        return isinstance(element, DB.ImportInstance)

    def AllowReference(self, reference, position):
        return False


class HostFilter(ISelectionFilter):
    def AllowElement(self, element):
        return is_host(element)

    def AllowReference(self, reference, position):
        return False


class RollBackOnError(DB.IFailuresPreprocessor):
    def __init__(self):
        self.message = None

    def PreprocessFailures(self, accessor):
        for failure in accessor.GetFailureMessages():
            if failure.GetSeverity() == DB.FailureSeverity.Error:
                self.message = failure.GetDescriptionText()
                return DB.FailureProcessingResult.ProceedWithRollBack
        return DB.FailureProcessingResult.Continue


def pick(uidoc, selection_filter, prompt):
    picked = uidoc.Selection.PickObject(ObjectType.Element, selection_filter, prompt)
    return uidoc.Document.GetElement(picked.ElementId)


def selected_cad(uidoc):
    ids = list(uidoc.Selection.GetElementIds())
    if len(ids) == 1:
        element = uidoc.Document.GetElement(ids[0])
        if isinstance(element, DB.ImportInstance):
            return element
    return pick(uidoc, CadFilter(), u"Выберите размещённую подложку DWG.")


def find_material(doc, name):
    for material in DB.FilteredElementCollector(doc).OfClass(DB.Material):
        if DB.Element.Name.GetValue(material).lower() == name.lower():
            return material.Id
    return None


def find_parameter(element, name):
    parameters = list(element.GetParameters(name))
    if len(parameters) > 1:
        raise ValueError(u"У области несколько параметров с именем «{}».".format(name))
    return parameters[0] if parameters else None


def target_parameters(element, settings):
    targets = {}
    if settings.region_name:
        targets[NAME] = find_parameter(element, NAME)
    if settings.ifc_class:
        existing = [(name, find_parameter(element, name)) for name in IFC_ALIASES]
        targets.update((name, parameter) for name, parameter in existing if parameter is not None)
        if not any(name in targets for name in IFC_ALIASES):
            targets[IFC] = None
    if settings.write_area:
        targets[AREA] = find_parameter(element, AREA)
    return targets


def bind_missing_parameters(doc, category, names):
    if not names:
        return
    existing_names = set(p.GetDefinition().Name
                         for p in DB.FilteredElementCollector(doc).OfClass(DB.ParameterElement)
                         if p.GetDefinition() is not None)
    for name in names:
        if name in existing_names:
            raise ValueError(u"В проекте уже есть параметр «{}». Добавьте категорию"
                             u" «Топография» в его привязку по экземпляру.".format(name))

    app = doc.Application
    previous = app.SharedParametersFilename
    fd, path = tempfile.mkstemp(suffix=".txt")
    try:
        with os.fdopen(fd, "w") as shared_file:
            shared_file.write("# Revit shared parameter file\n*META\tVERSION\tMINVERSION\n"
                              "META\t2\t1\n*GROUP\tID\tNAME\n"
                              "*PARAM\tGUID\tNAME\tDATATYPE\tDATACATEGORY\tGROUP\tVISIBLE"
                              "\tDESCRIPTION\tUSERMODIFIABLE\tHIDEWHENNOVALUE\n")
        app.SharedParametersFilename = path
        definitions = app.OpenSharedParameterFile()
        if definitions is None:
            raise ValueError(u"Revit не открыл временный файл общих параметров.")
        group = definitions.Groups.Create("A101 Landscape")
        categories = app.Create.NewCategorySet()
        categories.Insert(category)
        for name in names:
            parameter_type, guid = PARAMETERS[name]
            options = DB.ExternalDefinitionCreationOptions(name, parameter_type)
            options.GUID = Guid(guid)
            definition = group.Definitions.Create(options)
            binding = app.Create.NewInstanceBinding(categories)
            if not doc.ParameterBindings.Insert(definition, binding, DB.BuiltInParameterGroup.PG_DATA):
                raise ValueError(u"Revit не привязал параметр «{}» к топографии.".format(name))
    finally:
        app.SharedParametersFilename = previous
        if os.path.exists(path):
            os.remove(path)


def set_parameters(element, settings):
    projected = element.get_Parameter(DB.BuiltInParameter.PROJECTED_SURFACE_AREA)
    if projected is None or not projected.HasValue or projected.AsDouble() <= 0:
        raise ValueError(u"Revit не вычислил спроецированную площадь области.")
    area = projected.AsDouble()

    for name, parameter in target_parameters(element, settings).items():
        if parameter is None or parameter.IsReadOnly:
            raise ValueError(u"Параметр «{}» недоступен для записи.".format(name))
        if name == AREA:
            spec = (parameter.Definition.GetDataType() if hasattr(parameter.Definition, "GetDataType")
                    else parameter.Definition.GetSpecTypeId())
            if parameter.StorageType != DB.StorageType.Double or not spec.Equals(DB.SpecTypeId.Area):
                raise ValueError(u"Параметр «{}» должен иметь тип данных «Площадь».".format(name))
            previous_value = parameter.AsDouble()
            value = area
        else:
            if parameter.StorageType != DB.StorageType.String:
                raise ValueError(u"Параметр «{}» должен иметь тип данных «Текст».".format(name))
            previous_value = parameter.AsString()
            value = settings.region_name if name == NAME else settings.ifc_class
        if previous_value != value and not parameter.Set(value):
            raise ValueError(u"Revit не записал значение параметра «{}».".format(name))
    return area


def option_label(option):
    return option.Name if option is not None else u"Основная модель"


def host_label(host):
    return u"ID {} ({})".format(host.Id.IntegerValue, option_label(host.DesignOption))


def probe_host(doc, profile, host):
    probe = DB.SubTransaction(doc)
    region = None
    try:
        if probe.Start() != DB.TransactionStatus.Started:
            raise ValueError(u"Revit не начал проверку топоповерхности.")
        try:
            region = SiteSubRegion.Create(doc, profile, host.Id)
        except (ArgumentException, InvalidOperationException) as error:
            return u"{}".format(error)
        return None
    finally:
        try:
            if region is not None:
                region.Dispose()
            if probe.GetStatus() == DB.TransactionStatus.Started:
                if probe.RollBack() != DB.TransactionStatus.RolledBack:
                    raise ValueError(u"Revit не завершил откат пробной области.")
        finally:
            probe.Dispose()


def plan_regions(doc, preferred, profiles):
    alternatives = []
    for element in DB.FilteredElementCollector(doc, doc.ActiveView.Id).OfClass(TopographySurface):
        if element.Id != preferred.Id and is_host(element):
            box = element.get_BoundingBox(None)
            extent = (box.Min.X, box.Min.Y, box.Max.X, box.Max.Y) if box is not None else None
            alternatives.append((element, extent))

    transaction = DB.Transaction(doc, u"Проверка привязки областей")
    plans = []
    rejected = []
    try:
        if transaction.Start() != DB.TransactionStatus.Started:
            raise ValueError(u"Revit не начал проверку привязки областей.")
        options = transaction.GetFailureHandlingOptions()
        options.SetClearAfterRollback(True)
        transaction.SetFailureHandlingOptions(options)
        for index, profile in enumerate(profiles, 1):
            reason = probe_host(doc, profile, preferred)
            if reason is None:
                plans.append((index, profile, preferred))
                continue

            extent = geometry.bounds(profile[0])
            compatible = [host for host, box in alternatives
                          if (box is None or geometry.bounds_overlap(extent, box))
                          and probe_host(doc, profile, host) is None]
            if len(compatible) == 1:
                plans.append((index, profile, compatible[0]))
            elif compatible:
                rejected.append((index, u"Подходят несколько основ ({})".format(
                    u", ".join(host_label(host) for host in compatible))))
            else:
                rejected.append((index, reason))
    finally:
        try:
            if transaction.GetStatus() == DB.TransactionStatus.Started:
                if transaction.RollBack() != DB.TransactionStatus.RolledBack:
                    raise ValueError(u"Revit не завершил откат проверки привязки.")
        finally:
            transaction.Dispose()
    return plans, rejected


def create_regions(doc, plans, settings):
    transaction = DB.Transaction(doc, TITLE)
    failures = RollBackOnError()
    regions = []
    try:
        if transaction.Start() != DB.TransactionStatus.Started:
            raise ValueError(u"Revit не начал транзакцию.")
        options = transaction.GetFailureHandlingOptions()
        options.SetFailuresPreprocessor(failures)
        options.SetClearAfterRollback(True)
        options.SetForcedModalHandling(True)
        transaction.SetFailureHandlingOptions(options)
        material = find_material(doc, settings.material) if settings.material else None
        if settings.material and material is None:
            material = DB.Material.Create(doc, settings.material)

        for index, profile, host in plans:
            try:
                region = SiteSubRegion.Create(doc, profile, host.Id)
                regions.append(region)
                if material is not None:
                    region.TopographySurface.MaterialId = material
            except Exception as error:
                raise ValueError(u"Область №{} не создана на основе {}.\n{}".format(
                    index, host_label(host), error))

        doc.Regenerate()
        first = regions[0].TopographySurface
        missing = sorted(name for name, param in target_parameters(first, settings).items()
                         if param is None)
        if missing:
            bind_missing_parameters(doc, first.Category, missing)
            doc.Regenerate()
        total_area = sum(set_parameters(region.TopographySurface, settings) for region in regions)
        ids = [region.TopographySurface.Id for region in regions]
        if transaction.Commit() != DB.TransactionStatus.Committed:
            raise ValueError(failures.message or u"Revit отклонил создание областей.")
        return ids, total_area
    finally:
        if transaction.GetStatus() == DB.TransactionStatus.Started:
            transaction.RollBack()
        transaction.Dispose()
        for region in regions:
            region.Dispose()


def main():
    uidoc = revit.uidoc
    if uidoc is None or uidoc.Document.IsFamilyDocument:
        forms.alert(u"Откройте проект Revit.", title=TITLE)
        return
    doc = uidoc.Document

    try:
        cad = selected_cad(uidoc)
        layers = geometry.cad_layers(doc, cad)
        if not layers:
            raise ValueError(u"В подложке не найдены линии или заливки DWG.")
        settings = SettingsWindow(layers)
        if not settings.show_dialog():
            return

        items = layers[settings.layer_name]
        tolerance = doc.Application.ShortCurveTolerance
        open_edges = 0
        ambiguous = []
        invalid = []
        excluded = 0
        if settings.read_fills:
            loops = geometry.fill_loops(items, tolerance)
        else:
            loops, open_edges, ambiguous = geometry.outline_loops(items, tolerance)
            if loops:
                loops, invalid = geometry.partition_outline_loops(loops)

        details = []
        if open_edges:
            details.append(u"Отдельных сегментов вне замкнутых контуров: {}.".format(open_edges))
        if ambiguous:
            details.append(u"Групп сегментов с неоднозначной границей: {}.".format(len(ambiguous)))
        if invalid:
            details.append(u"Пересекающихся, касающихся или недопустимых контуров: {}.".format(len(invalid)))
        if not loops:
            if details:
                raise ValueError(u"Не найдено однозначных допустимых контуров.\n" + u"\n".join(details))
            raise ValueError(u"Revit не отдал замкнутых границ в выбранном режиме."
                             u" Попробуйте другой режим или вынесите замкнутые"
                             u" полилинии границ в отдельный слой DWG.")

        profiles = geometry.separate_regions(loops)
        if ambiguous or invalid:
            safe_profiles = geometry.exclude_uncertain_profiles(profiles, ambiguous + invalid)
            excluded = len(profiles) - len(safe_profiles)
            profiles = safe_profiles
        if excluded:
            details.append(u"Дополнительно исключено областей рядом со спорными контурами: {}.".format(excluded))
        if not profiles:
            raise ValueError(u"Все найденные области затрагивают спорные границы.\n" + u"\n".join(details))

        host = pick(uidoc, HostFilter(), u"Выберите основную топоповерхность для новых областей.")
        active_id = DB.DesignOption.GetActiveDesignOptionId(doc)
        active_option = doc.GetElement(active_id) if active_id != DB.ElementId.InvalidElementId else None
        host_context = u"Выбранная топоповерхность: {}.\nАктивный вариант: {}.".format(
            host_label(host), option_label(active_option))
        plans, rejected = plan_regions(doc, host, profiles)
        if rejected:
            details.append(u"Не удалось однозначно привязать областей: {}.".format(len(rejected)))
            for number, reason in rejected[:2]:
                details.append(u"№{}: {}".format(number, reason))
        if not plans:
            raise ValueError(u"Проверка не нашла подходящей основы для областей.\n{}\n\n{}".format(
                host_context, u"\n".join(details)))

        host_counts = {}
        for _, _, target in plans:
            label = host_label(target)
            host_counts[label] = host_counts.get(label, 0) + 1
        summary = u"Будет создано отдельных областей: {}.\nСлой DWG: {}.".format(
            len(plans), settings.layer_name)
        summary += u"\n\n{}\nРаспределение по топоповерхностям:\n{}".format(
            host_context, u"\n".join(u"{} — {} обл.".format(label, count)
                                    for label, count in sorted(host_counts.items())))
        if details:
            summary += u"\n\nБудет обработана только однозначная часть слоя.\n" + u"\n".join(details)
        summary += u"\n\nЕсли заданных параметров или материала нет, команда добавит их в проект."
        if not forms.alert(summary, title=TITLE, yes=True, no=True):
            return

        ids, total_area = create_regions(doc, plans, settings)
        selected = List[DB.ElementId]()
        for element_id in ids:
            selected.Add(element_id)
        uidoc.Selection.SetElementIds(selected)
        area_m2 = DB.UnitUtils.ConvertFromInternalUnits(total_area, DB.UnitTypeId.SquareMeters)
        forms.alert(u"Создано областей: {}.\nСпроецированная площадь: {:.2f} м².".format(
            len(ids), area_m2), title=TITLE)
    except OperationCanceledException:
        return
    except Exception as error:
        forms.alert(u"{}\n\n{}".format(TITLE, error), title=TITLE)


if __name__ == "__main__":
    main()
