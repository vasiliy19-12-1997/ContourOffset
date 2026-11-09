# -*- coding: utf-8 -*-
"""Reads horizontal fill and outline geometry from a placed CAD import."""

from collections import defaultdict

from Autodesk.Revit import DB
from Autodesk.Revit.DB.Architecture import SiteSubRegion
from Autodesk.Revit.Exceptions import ArgumentException, InvalidOperationException
from System.Collections.Generic import List


JOIN_TOLERANCE = 1e-6  # feet
NORMAL = DB.XYZ.BasisZ


def read_cad_geometry(document, cad, options):
    layers = defaultdict(list)
    layer_names = {}

    def layer_name(style_id):
        if style_id not in layer_names:
            style = document.GetElement(style_id) if style_id != DB.ElementId.InvalidElementId else None
            category = style.GraphicsStyleCategory if isinstance(style, DB.GraphicsStyle) else None
            layer_names[style_id] = category.Name if category is not None else None
        return layer_names[style_id]

    def walk(geometry, transform, inherited):
        if geometry is None:
            return
        for item in geometry:
            name = layer_name(item.GraphicsStyleId)
            layer = name if name and (name != "0" or not inherited) else inherited
            if isinstance(item, DB.GeometryInstance):
                walk(item.GetSymbolGeometry(), transform.Multiply(item.Transform), layer)
            elif isinstance(item, (DB.Curve, DB.PolyLine)) or is_fill(item):
                layers[layer or u"(без слоя)"].append((item, transform))

    walk(cad.get_Geometry(options), DB.Transform.Identity, None)
    return layers


def cad_layers(document, cad):
    layers = read_cad_geometry(document, cad, DB.Options())
    category = cad.Category
    subcategories = list(category.SubCategories) if category is not None else []
    if not subcategories:
        cad_type = document.GetElement(cad.GetTypeId())
        category = cad_type.Category if cad_type is not None else None
        subcategories = list(category.SubCategories) if category is not None else []

    # Revit lists CAD subcategories even when the import exposes no curves or faces.
    for layer in subcategories:
        layers.setdefault(layer.Name, [])
    view = document.ActiveView
    missing_visible = [layer.Name for layer in subcategories
                       if not layers[layer.Name] and not view.GetCategoryHidden(layer.Id)]
    if missing_visible:
        try:
            options = DB.Options()
            options.View = view
            view_layers = read_cad_geometry(document, cad, options)
        except (ArgumentException, InvalidOperationException):
            return layers
        for name in missing_visible:
            if name in view_layers:
                layers[name] = view_layers[name]
    return layers


def is_fill(item):
    return isinstance(item, DB.Mesh) or (isinstance(item, DB.Solid) and item.Faces.Size > 0)


def key(point):
    return (int(round(point.X / JOIN_TOLERANCE)), int(round(point.Y / JOIN_TOLERANCE)))


def xy(point):
    return DB.XYZ(point.X, point.Y, 0)


def flat_points(points):
    if max(point.Z for point in points) - min(point.Z for point in points) > JOIN_TOLERANCE:
        raise ValueError(u"На слое есть наклонная геометрия. Нужны горизонтальные контуры.")
    return [xy(point) for point in points]


def flatten_curve(curve, transform):
    world = curve.CreateTransformed(transform)
    if not world.IsBound or (world.IsCyclic and world.GetEndPoint(0).IsAlmostEqualTo(world.GetEndPoint(1))):
        if not world.IsCyclic:
            raise ValueError(u"На слое есть неограниченная кривая.")
        start = world.GetEndParameter(0) if world.IsBound else 0.0
        end = world.GetEndParameter(1) if world.IsBound else world.Period
        middle = (start + end) / 2.0
        parts = []
        for low, high in ((start, middle), (middle, end)):
            part = world.Clone()
            part.MakeBound(low, high)
            parts.extend(flatten_curve(part, DB.Transform.Identity))
        return parts

    points = list(world.Tessellate())
    flat_points(points)
    return [world.CreateTransformed(DB.Transform.CreateTranslation(DB.XYZ(0, 0, -points[0].Z)))]


def line_path(points, min_length):
    clean = []
    for point in points:
        if clean and key(point) == key(clean[-1]):
            continue
        while len(clean) >= 2:
            first = clean[-1].Subtract(clean[-2])
            second = point.Subtract(clean[-1])
            if (first.DotProduct(second) <= 0
                    or first.CrossProduct(second).GetLength() >
                    JOIN_TOLERANCE * (first.GetLength() + second.GetLength())):
                break
            clean.pop()
        clean.append(point)
    curves = []
    for a, b in zip(clean, clean[1:]):
        if a.DistanceTo(b) <= min_length:
            raise ValueError(u"В контуре есть отрезок короче допустимого для Revit.")
        curves.append(DB.Line.CreateBound(a, b))
    return curves


def closed_chains(edges):
    """Orders indexed endpoint pairs into closed, unbranched chains."""
    adjacent = defaultdict(list)
    for index, (a, b) in enumerate(edges):
        adjacent[a].append(index)
        adjacent[b].append(index)
    if any(len(indices) > 2 for indices in adjacent.values()):
        raise ValueError(u"Оставшиеся отдельные линии образуют разветвление."
                         u" В DWG объедините сегменты каждого участка в отдельную"
                         u" замкнутую полилинию и обновите подложку.")

    used = set()
    rings = []
    for start in adjacent:
        node = start
        chain = []
        while True:
            remaining = [index for index in adjacent[node] if index not in used]
            if not remaining:
                break
            index = remaining[0]
            used.add(index)
            a, b = edges[index]
            forward = node == a
            chain.append((index, forward))
            node = b if forward else a
        if not chain:
            continue
        if node != start:
            raise ValueError(u"Найдена незамкнутая граница.")
        rings.append(chain)
    return rings


def edge_blocks(edges):
    """Split an undirected graph at cut vertices without a recursive walk."""
    adjacent = defaultdict(list)
    for index, (a, b) in enumerate(edges):
        adjacent[a].append(index)
        adjacent[b].append(index)

    order = {}
    low = {}
    pending = []
    for root in adjacent:
        if root in order:
            continue
        order[root] = low[root] = len(order)
        walk = [(root, None, iter(adjacent[root]))]
        while walk:
            node, parent_edge, incident = walk[-1]
            edge = next(incident, None)
            if edge is None:
                walk.pop()
                if parent_edge is None:
                    continue
                parent = walk[-1][0]
                low[parent] = min(low[parent], low[node])
                if low[node] >= order[parent]:
                    block = [pending.pop()]
                    while block[-1] != parent_edge:
                        block.append(pending.pop())
                    yield block
                continue

            if edge == parent_edge:
                continue
            a, b = edges[edge]
            other = b if node == a else a
            if other == node:
                continue
            if other not in order:
                pending.append(edge)
                order[other] = low[other] = len(order)
                walk.append((other, edge, iter(adjacent[other])))
            elif order[other] < order[node]:
                pending.append(edge)
                low[node] = min(low[node], order[other])


def outline_rings(curves):
    edges = [endpoints(curve) for curve in curves]
    rings = []
    open_edges = sum(a == b for a, b in edges)
    ambiguous = []
    for block in edge_blocks(edges):
        if len(block) == 1:
            open_edges += 1
            continue

        local_edges = [edges[index] for index in block]
        degree = defaultdict(int)
        for a, b in local_edges:
            degree[a] += 1
            degree[b] += 1
        if any(value != 2 for value in degree.values()):
            ambiguous.append([curves[index] for index in block])
            continue

        for ring in closed_chains(local_edges):
            rings.append([
                curves[block[index]] if forward else curves[block[index]].CreateReversed()
                for index, forward in ring
            ])
    return rings, open_edges, ambiguous


def make_loop(curves):
    net_curves = List[DB.Curve]()
    for curve in curves:
        net_curves.Add(curve)
    return DB.CurveLoop.Create(net_curves)


def endpoints(curve):
    return key(curve.GetEndPoint(0)), key(curve.GetEndPoint(1))


def edge_fingerprint(curve):
    ends = tuple(sorted(endpoints(curve)))
    if isinstance(curve, DB.Line):
        return ("line", ends)
    if isinstance(curve, DB.Arc):
        return ("arc", ends, key(curve.Evaluate(0.5, True)))
    return None


def outline_loops(items, min_length):
    closed_paths = []
    loose_curves = []
    for item, transform in items:
        if isinstance(item, DB.PolyLine):
            points = flat_points([transform.OfPoint(point) for point in item.GetCoordinates()])
            curves = line_path(points, min_length)
        elif isinstance(item, DB.Curve):
            curves = flatten_curve(item, transform)
        else:
            continue
        if len(curves) > 1 and endpoints(curves[0])[0] == endpoints(curves[-1])[1]:
            closed_paths.append(curves)
        else:
            loose_curves.extend(curves)

    loops = []
    known_rings = set()
    known_edges = set()
    for path in closed_paths:
        fingerprints = [edge_fingerprint(curve) for curve in path]
        if all(fingerprint is not None for fingerprint in fingerprints):
            ring = tuple(sorted(fingerprints))
            if ring in known_rings:
                continue
            known_rings.add(ring)
        loops.append(make_loop(path))
        known_edges.update(fingerprint for fingerprint in fingerprints if fingerprint is not None)

    unique_curves = []
    for curve in loose_curves:
        fingerprint = edge_fingerprint(curve)
        if fingerprint is not None:
            if fingerprint in known_edges:
                continue
            known_edges.add(fingerprint)
        unique_curves.append(curve)

    rings, open_edges, ambiguous = outline_rings(unique_curves)
    for ring in rings:
        loops.append(make_loop(ring))
    return loops, open_edges, ambiguous


def bounds(curves):
    points = [point for curve in curves for point in curve.Tessellate()]
    return (min(point.X for point in points), min(point.Y for point in points),
            max(point.X for point in points), max(point.Y for point in points))


def bounds_overlap(first, second):
    x1, y1, x2, y2 = first
    left, bottom, right, top = second
    return left <= x2 and x1 <= right and bottom <= y2 and y1 <= top


def partition_outline_loops(loops):
    if SiteSubRegion.IsValidBoundary(net_loops(loops)):
        return loops, []

    boxes = [bounds(loop) for loop in loops]
    invalid = set(index for index, loop in enumerate(loops)
                  if not SiteSubRegion.IsValidBoundary(net_loops([loop])))
    valid_singles = [index for index in range(len(loops)) if index not in invalid]
    for position, index in enumerate(valid_singles):
        for other in valid_singles[position + 1:]:
            if (bounds_overlap(boxes[index], boxes[other])
                    and not SiteSubRegion.IsValidBoundary(net_loops([loops[index], loops[other]]))):
                invalid.update((index, other))
    return ([loop for index, loop in enumerate(loops) if index not in invalid],
            [loop for index, loop in enumerate(loops) if index in invalid])


def exclude_uncertain_profiles(profiles, ambiguous):
    if not ambiguous:
        return profiles
    uncertain_bounds = [bounds(curves) for curves in ambiguous]
    safe = []
    for profile in profiles:
        profile_bounds = bounds(profile[0])
        if not any(bounds_overlap(profile_bounds, uncertain) for uncertain in uncertain_bounds):
            safe.append(profile)
    return safe


def mesh_loops(meshes, min_length):
    points_by_key = {}
    triangles = set()
    uses = defaultdict(int)
    for mesh, transform in meshes:
        for index in range(mesh.NumTriangles):
            triangle = mesh.get_Triangle(index)
            points = flat_points([transform.OfPoint(triangle.get_Vertex(i)) for i in range(3)])
            vertices = [key(point) for point in points]
            if len(set(vertices)) < 3:
                continue
            fingerprint = tuple(sorted(vertices))
            if fingerprint in triangles:
                continue
            triangles.add(fingerprint)
            for vertex, point in zip(vertices, points):
                points_by_key.setdefault(vertex, point)
            for a, b in zip(vertices, vertices[1:] + vertices[:1]):
                uses[tuple(sorted((a, b)))] += 1
    if any(count > 2 for count in uses.values()):
        raise ValueError(u"Треугольники заливки накладываются друг на друга.")

    edges = sorted(edge for edge, count in uses.items() if count == 1)
    loops = []
    for ring in closed_chains(edges):
        vertices = [points_by_key[edges[index][0 if forward else 1]]
                    for index, forward in ring]
        loops.append(make_loop(line_path(vertices + vertices[:1], min_length)))
    return loops


def fill_loops(items, min_length):
    loops = []
    meshes = []
    for item, transform in items:
        if isinstance(item, DB.Mesh):
            meshes.append((item, transform))
        elif isinstance(item, DB.Solid):
            up = []
            down = []
            for face in item.Faces:
                if not isinstance(face, DB.PlanarFace):
                    continue
                slope = transform.OfVector(face.FaceNormal).Normalize().DotProduct(NORMAL)
                if slope > 0.999999:
                    up.append(face)
                elif slope < -0.999999:
                    down.append(face)
            if item.Faces.Size and not (up or down):
                raise ValueError(u"У заливки нет горизонтальной грани.")
            for face in up or down:
                for loop in face.GetEdgesAsCurveLoops():
                    curves = [part for curve in loop for part in flatten_curve(curve, transform)]
                    loops.append(make_loop(curves))
    return loops + mesh_loops(meshes, min_length)


def net_loops(loops):
    result = List[DB.CurveLoop]()
    for loop in loops:
        result.Add(loop)
    return result


def separate_regions(loops):
    """Returns one profile per outer ring, with its immediate holes."""
    if not SiteSubRegion.IsValidBoundary(net_loops(loops)):
        raise ValueError(u"Контуры пересекаются, касаются друг друга или имеют разрыв.")

    solids = [DB.GeometryCreationUtilities.CreateExtrusionGeometry(net_loops([loop]), NORMAL, 1.0)
              for loop in loops]
    caps = [
        next(face for face in solid.Faces if isinstance(face, DB.PlanarFace)
             and face.FaceNormal.DotProduct(NORMAL) > 0.999999)
        for solid in solids
    ]
    containers = []
    for index, loop in enumerate(loops):
        point = next(iter(loop)).GetEndPoint(0)
        containers.append([other for other, cap in enumerate(caps)
                           if index != other and cap.Project(point) is not None])

    profiles = {}
    for index, parents in enumerate(containers):
        if len(parents) % 2 == 0:
            profiles[index] = net_loops([loops[index]])
    for index, parents in enumerate(containers):
        if len(parents) % 2 == 1:
            parent = max(parents, key=lambda candidate: len(containers[candidate]))
            profiles[parent].Add(loops[index])
    return [profiles[index] for index in sorted(profiles)]
