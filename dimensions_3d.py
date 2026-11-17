# SPDX-License-Identifier: GPL-3.0-or-later
"""Create exportable 3D measurements from selected IngeTrazo geometry."""

from __future__ import annotations

import logging

from PySide6.QtGui import QColor, QMatrix4x4, QVector3D
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QColorDialog,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QFontComboBox,
    QInputDialog,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from core.group import Group
from core.history import Command
from core.layers import DEFAULT_LAYER, active_layer
from core.mesh import Edge, Face, Mesh

KEY = "dimensions_3d"
COLOR = (0.05, 0.38, 0.95)
log = logging.getLogger("ingetrazo.plugins.dimensions_3d")
_LIVE_LINKS: dict[str, list[dict]] = {}


def _copy_point(point) -> QVector3D:
    return QVector3D(point)


def _unit(vector: QVector3D) -> QVector3D:
    length = vector.length()
    if length <= 1e-12:
        raise ValueError("A zero-length direction cannot define a measurement.")
    return vector / length


def _point(matrix: QMatrix4x4, value) -> QVector3D:
    return matrix.map(_copy_point(value))


def _identity() -> QMatrix4x4:
    return QMatrix4x4()


def _walk_group(group: Group, path=(), matrix=None):
    matrix = QMatrix4x4(matrix) if matrix is not None else _identity()
    if group.xform is not None:
        matrix = matrix * group.xform
    current_path = (*path, group.uid)
    yield group, current_path, matrix
    for child in group.children or ():
        yield from _walk_group(child, current_path, matrix)


def _mesh_locations(scene) -> list[dict]:
    locations = []
    seen = set()
    edit_group = getattr(scene, "edit_group", None)
    for root_name in ("loose", "scene"):
        mesh = getattr(scene, "loose_mesh" if root_name == "loose" else "mesh", None)
        if root_name == "scene" and edit_group is not None \
                and mesh is getattr(edit_group, "mesh", None):
            continue
        if mesh is not None and id(mesh) not in seen:
            seen.add(id(mesh))
            locations.append({
                "mesh": mesh,
                "root": root_name,
                "path": [],
                "matrix": _identity(),
            })
    for group in scene.groups:
        for placed, path, matrix in _walk_group(group):
            locations.append({
                "mesh": placed.mesh,
                "root": "groups",
                "path": list(path),
                "matrix": matrix,
            })
    return locations


def _group_locations(scene):
    def walk(container, parent_path=()):
        for index, group in enumerate(list(container)):
            path = (*parent_path, group.uid)
            yield group, container, index, list(path)
            yield from walk(group.children or [], path)

    yield from walk(scene.groups)


def _find_group_path(scene, path: list[str]):
    container = scene.groups
    groups = []
    matrix = _identity()
    found = None
    for uid in path:
        found = next((group for group in container if group.uid == uid), None)
        if found is None:
            raise ValueError("A source group for a 3D measurement no longer exists.")
        groups.append(found)
        if found.xform is not None:
            matrix = matrix * found.xform
        container = found.children or []
    if found is None:
        raise ValueError("A 3D measurement has no source group.")
    return found, groups, matrix


def _reference_for_item(scene, kind: str, item) -> dict:
    matches = []
    for location in _mesh_locations(scene):
        values = location["mesh"].edges if kind == "edge" else location["mesh"].faces
        try:
            index = values.index(item)
        except ValueError:
            continue
        matches.append((location, index))
    if len(matches) != 1:
        if not matches:
            raise ValueError("Could not locate selected geometry in the document.")
        raise ValueError(
            "The selected geometry belongs to multiple component placements. "
            "Edit or make the component unique before measuring it."
        )
    location, index = matches[0]
    mesh = location["mesh"]
    reference = {
        "kind": kind,
        "root": location["root"],
        "path": location["path"],
        "index": index,
        "topology": {
            "vertices": len(mesh.vertices),
            "edges": len(mesh.edges),
            "faces": len(mesh.faces),
        },
    }
    if kind == "face":
        reference["loop_size"] = len(item.loop)
        reference["hole_count"] = len(item.hole_loops)
    return reference


def _resolve_reference(scene, reference: dict, live=None) -> dict:
    kind = reference.get("kind")
    if kind == "solid":
        group, groups, matrix = _find_group_path(scene, reference.get("path", []))
        return {
            "reference": reference,
            "item": group,
            "groups": groups,
            "matrix": matrix,
            "live": live,
        }

    if kind not in {"edge", "face"}:
        raise ValueError("A 3D measurement contains an unknown source type.")
    path = reference.get("path", [])
    if path:
        _group, groups, matrix = _find_group_path(scene, path)
        mesh = groups[-1].mesh
    else:
        groups = []
        matrix = _identity()
        root_name = reference.get("root", "scene")
        mesh = getattr(scene, "loose_mesh" if root_name == "loose" else "mesh", None)
        if mesh is None:
            raise ValueError("The source mesh for a 3D measurement no longer exists.")

    collection = mesh.edges if kind == "edge" else mesh.faces
    item = None
    if live is not None and live.get("mesh") is mesh:
        candidate = live.get("item")
        if candidate in collection:
            item = candidate
        elif candidate is not None:
            raise ValueError(
                "The measured edge or face was replaced by a geometry edit. "
                "Recreate its 3D measurement from the current selection."
            )
    if item is None:
        topology = reference.get("topology")
        current_topology = {
            "vertices": len(mesh.vertices),
            "edges": len(mesh.edges),
            "faces": len(mesh.faces),
        }
        if topology is not None and topology != current_topology:
            raise ValueError(
                "The source mesh topology has changed since this measurement "
                "was saved. Recreate the 3D measurement from the current selection."
            )
        index = reference.get("index")
        if not isinstance(index, int) or index < 0 or index >= len(collection):
            raise ValueError(
                "A measured edge or face no longer exists. Recreate its 3D measurement."
            )
        item = collection[index]
        if kind == "face" and (
            len(item.loop) != reference.get("loop_size")
            or len(item.hole_loops) != reference.get("hole_count")
        ):
            raise ValueError(
                "The measured face topology changed. Recreate its 3D measurement."
            )
    return {
        "reference": reference,
        "item": item,
        "mesh": mesh,
        "groups": groups,
        "matrix": matrix,
        "live": live,
    }


def _selected_sources(scene) -> tuple[str, list[dict]]:
    selection = list(scene.selection)
    if not selection:
        raise ValueError("Select edges, faces, or solid groups first.")

    if all(isinstance(item, Edge) for item in selection):
        kind = "edge"
    elif all(isinstance(item, Face) for item in selection):
        kind = "face"
    elif all(isinstance(item, Group) for item in selection):
        kind = "solid"
    else:
        raise ValueError(
            "Select only edges, only faces, or only solid groups at a time."
        )

    sources = []
    seen = set()
    for item in selection:
        if id(item) in seen:
            continue
        seen.add(id(item))
        if kind == "solid":
            matches = [
                (group, path)
                for group, _container, _index, path in _group_locations(scene)
                if group is item
            ]
            if len(matches) != 1:
                raise ValueError("Could not locate a selected group in the document.")
            _group, path = matches[0]
            reference = {"kind": "solid", "path": path}
            live = {"item": item}
        else:
            reference = _reference_for_item(scene, kind, item)
            live = {
                "item": item,
                "mesh": next(
                    location["mesh"]
                    for location in _mesh_locations(scene)
                    if location["path"] == reference["path"]
                    and location["root"] == reference["root"]
                    and item in (
                        location["mesh"].edges
                        if kind == "edge"
                        else location["mesh"].faces
                    )
                ),
            }
        sources.append({"reference": reference, "live": live})
    return kind, sources


def _world_triangles(face: Face, matrix: QMatrix4x4):
    return [
        tuple(_point(matrix, point) for point in triangle)
        for triangle in face.triangulate()
    ]


def _world_face_geometry(
    face: Face, matrix: QMatrix4x4
) -> tuple[float, QVector3D, QVector3D] | None:
    area = 0.0
    weighted_center = QVector3D()
    normal_sum = QVector3D()
    for a, b, c in _world_triangles(face, matrix):
        triangle_normal = QVector3D.crossProduct(b - a, c - a)
        triangle_area = triangle_normal.length() * 0.5
        area += triangle_area
        weighted_center += (a + b + c) * (triangle_area / 3.0)
        normal_sum += triangle_normal * 0.5
    if area <= 1e-12 or normal_sum.length() <= 1e-12:
        return None
    return area, weighted_center / area, _unit(normal_sum)


def _face_measure(source: dict) -> tuple[float, QVector3D]:
    geometry = _world_face_geometry(source["item"], source["matrix"])
    if geometry is None:
        raise ValueError("A selected face has zero area.")
    area, center, _normal = geometry
    return area, center


def _visible_face_center(
    faces: list[dict],
    view_basis: tuple[QVector3D, QVector3D, QVector3D],
) -> QVector3D:
    if not faces:
        raise ValueError("No solid face is available for the volume leader line.")
    normal = view_basis[2]
    return max(
        faces,
        key=lambda face: (
            abs(QVector3D.dotProduct(face["normal"], normal)),
            QVector3D.dotProduct(face["center"], normal),
        ),
    )["center"]


def _mesh_world_volume(mesh: Mesh, matrix: QMatrix4x4) -> float | None:
    boundary = [face for face in mesh.faces if not face.interior]
    if not boundary:
        return None
    for edge in mesh.edges:
        incident = [face for face in edge.faces if not face.interior]
        if incident and len(incident) != 2:
            return None

    pending = set(boundary)
    total = 0.0
    while pending:
        seed = pending.pop()
        component = [seed]
        stack = [seed]
        while stack:
            face = stack.pop()
            loops = (face.loop, *face.hole_loops)
            for vertex in (vertex for loop in loops for vertex in loop):
                for edge in vertex.edges:
                    for neighbor in edge.faces:
                        if neighbor in pending and not neighbor.interior:
                            pending.remove(neighbor)
                            component.append(neighbor)
                            stack.append(neighbor)
        origin = _point(matrix, component[0].vertices[0])
        signed = 0.0
        for face in component:
            for a, b, c in _world_triangles(face, matrix):
                a, b, c = a - origin, b - origin, c - origin
                signed += QVector3D.dotProduct(
                    a, QVector3D.crossProduct(b, c)
                ) / 6.0
        total += abs(signed)
    return total


def _solid_measure(
    source: dict,
) -> tuple[float, QVector3D, list[dict]]:
    root = source["item"]
    base_path = source["groups"]
    total_volume = 0.0
    points = []
    visible_faces = []

    def visit(group: Group, parent_matrix: QMatrix4x4):
        matrix = QMatrix4x4(parent_matrix)
        if group.xform is not None:
            matrix = matrix * group.xform
        if group.mesh.faces and group.mesh.edges:
            volume = _mesh_world_volume(group.mesh, matrix)
            if volume is not None and volume > 1e-12:
                points.extend(_point(matrix, vertex.position)
                              for vertex in group.mesh.vertices)
                for face in group.mesh.faces:
                    if face.interior:
                        continue
                    geometry = _world_face_geometry(face, matrix)
                    if geometry is not None:
                        area, center, normal = geometry
                        visible_faces.append(
                            {"area": area, "center": center, "normal": normal}
                        )
                yield volume
        for child in group.children or ():
            yield from visit(child, matrix)

    parent_matrix = _identity()
    for group in base_path[:-1]:
        if group.xform is not None:
            parent_matrix = parent_matrix * group.xform
    total_volume = sum(visit(root, parent_matrix))
    if total_volume <= 1e-12 or not points:
        raise ValueError(
            f"Group '{root.name}' contains no closed solid mesh to measure."
        )
    center = sum(points, QVector3D()) / len(points)
    return total_volume, center, visible_faces


def _measure(source: dict, kind: str) -> dict:
    item = source["item"]
    matrix = source["matrix"]
    if kind == "edge":
        a = _point(matrix, item.a)
        b = _point(matrix, item.b)
        value = (b - a).length()
        if value <= 1e-12:
            raise ValueError("A selected edge has zero length.")
        return {"value": value, "center": (a + b) * 0.5, "a": a, "b": b}
    if kind == "face":
        value, center = _face_measure(source)
        return {"value": value, "center": center}
    value, center, visible_faces = _solid_measure(source)
    return {
        "value": value,
        "center": center,
        "visible_faces": visible_faces,
    }


def _group_world_geometry(source: dict) -> tuple[list[QVector3D], list[dict]]:
    root = source["item"]
    groups = source["groups"]
    points = []
    faces = []

    def visit(group: Group, parent_matrix: QMatrix4x4) -> None:
        matrix = QMatrix4x4(parent_matrix)
        if group.xform is not None:
            matrix = matrix * group.xform
        points.extend(
            _point(matrix, vertex.position) for vertex in group.mesh.vertices
        )
        for face in group.mesh.faces:
            if face.interior:
                continue
            geometry = _world_face_geometry(face, matrix)
            if geometry is not None:
                area, center, normal = geometry
                faces.append({"area": area, "center": center, "normal": normal})
        for child in group.children or ():
            visit(child, matrix)

    parent_matrix = _identity()
    for group in groups[:-1]:
        if group.xform is not None:
            parent_matrix = parent_matrix * group.xform
    visit(root, parent_matrix)
    if not points:
        raise ValueError(f"Group '{root.name}' contains no geometry to annotate.")
    return points, faces


def _outside_label_position(
    points: list[QVector3D],
    view_basis: tuple[QVector3D, QVector3D, QVector3D],
    offset: float,
    text_height: float,
    extrusion: float,
) -> tuple[QVector3D, QVector3D]:
    _right, up, normal = view_basis
    sign = -1.0 if offset < 0.0 else 1.0
    anchor = max(
        points,
        key=lambda point: sign * QVector3D.dotProduct(point, up),
    )
    front_depth = max(QVector3D.dotProduct(point, normal) for point in points)
    anchor_on_front = anchor + normal * (
        front_depth - QVector3D.dotProduct(anchor, normal)
    )
    clearance = max(abs(offset), text_height * 0.25, extrusion)
    text_base = anchor_on_front + normal * clearance
    if sign > 0.0:
        text_base += up * clearance
    else:
        text_base -= up * (clearance + text_height)
    return anchor_on_front, text_base


def _set_face_color(face, color: tuple[float, float, float] | None) -> None:
    if color is None:
        face.attrs.pop("color", None)
    else:
        face.attrs["color"] = list(color)
    face.attrs["back"] = True


def _add_face(mesh: Mesh, points,
              color: tuple[float, float, float] | None) -> None:
    face = mesh.add_face(points)
    _set_face_color(face, color)


def _add_box_beam(mesh: Mesh, start: QVector3D, end: QVector3D,
                  width: float,
                  color: tuple[float, float, float] | None) -> None:
    axis_vector = end - start
    if axis_vector.length() <= 1e-9 or width <= 0.0:
        return
    axis = _unit(axis_vector)
    up = QVector3D(0.0, 0.0, 1.0)
    side_vector = up - axis * QVector3D.dotProduct(up, axis)
    if side_vector.length() <= 1e-8:
        up = QVector3D(0.0, 1.0, 0.0)
        side_vector = up - axis * QVector3D.dotProduct(up, axis)
    side = _unit(side_vector)
    normal = _unit(QVector3D.crossProduct(axis, side))
    half_side = side * (width * 0.5)
    half_depth = normal * (width * 0.5)
    center = (start + end) * 0.5
    half_length = axis_vector * 0.5
    points = [
        center - half_length - half_side - half_depth,
        center + half_length - half_side - half_depth,
        center + half_length + half_side - half_depth,
        center - half_length + half_side - half_depth,
        center - half_length - half_side + half_depth,
        center + half_length - half_side + half_depth,
        center + half_length + half_side + half_depth,
        center - half_length + half_side + half_depth,
    ]
    for loop in (
        (4, 5, 6, 7),
        (0, 3, 2, 1),
        (0, 1, 5, 4),
        (1, 2, 6, 5),
        (2, 3, 7, 6),
        (3, 0, 4, 7),
    ):
        _add_face(mesh, [points[index] for index in loop], color)


def _callout_curve_points(
    start: QVector3D,
    end: QVector3D,
    style: str,
    view_basis: tuple[QVector3D, QVector3D, QVector3D],
    text_height: float,
) -> list[QVector3D]:
    delta = end - start
    length = delta.length()
    if length <= 1e-9 or style == "straight":
        return [start, end]

    right, up, normal = view_basis
    screen_delta = (
        right * QVector3D.dotProduct(delta, right)
        + up * QVector3D.dotProduct(delta, up)
    )
    if screen_delta.length() <= 1e-9:
        perpendicular = right
    else:
        perpendicular = _unit(
            QVector3D.crossProduct(normal, screen_delta)
        )
    bulge = min(length * 0.25, max(text_height * 1.5, length * 0.1))

    if style == "oblique":
        knee = start + delta * 0.65 + perpendicular * bulge
        return [start, knee, end]

    sign = -1.0 if style in {"concave", "concave-convex"} else 1.0
    steps = 20
    points = []
    if style in {"convex", "concave"}:
        control = (start + end) * 0.5 + perpendicular * (sign * bulge)
        for index in range(steps + 1):
            t = index / steps
            inverse = 1.0 - t
            points.append(
                start * (inverse * inverse)
                + control * (2.0 * inverse * t)
                + end * (t * t)
            )
        return points

    first_control = start + delta * (1.0 / 3.0) \
        + perpendicular * (sign * bulge)
    second_control = start + delta * (2.0 / 3.0) \
        - perpendicular * (sign * bulge)
    for index in range(steps + 1):
        t = index / steps
        inverse = 1.0 - t
        points.append(
            start * (inverse ** 3)
            + first_control * (3.0 * inverse * inverse * t)
            + second_control * (3.0 * inverse * t * t)
            + end * (t ** 3)
        )
    return points


def _add_callout_line(
    mesh: Mesh,
    start: QVector3D,
    end: QVector3D,
    style: str,
    view_basis: tuple[QVector3D, QVector3D, QVector3D],
    text_height: float,
    line_width: float,
    color: tuple[float, float, float] | None,
) -> None:
    points = _callout_curve_points(
        start, end, style, view_basis, text_height
    )
    for first, second in zip(points, points[1:]):
        _add_box_beam(mesh, first, second, line_width, color)


def _add_arrow(mesh: Mesh, tip: QVector3D, direction: QVector3D,
               side: QVector3D, normal: QVector3D, size: float,
               style: str, line_width: float,
               color: tuple[float, float, float] | None) -> None:
    if size <= 1e-9:
        return
    if style == "point":
        radius = size * 0.18
        top = tip + normal * radius
        bottom = tip - normal * radius
        left = tip + side * radius
        right = tip - side * radius
        front = tip + direction * radius
        back = tip - direction * radius
        for loop in (
            (top, front, left), (top, right, front),
            (top, back, right), (top, left, back),
            (bottom, left, front), (bottom, front, right),
            (bottom, right, back), (bottom, back, left),
        ):
            _add_face(mesh, list(loop), color)
        return
    if style == "oblique":
        slash = _unit(direction + side)
        half_length = size * 0.35
        _add_box_beam(
            mesh, tip - slash * half_length, tip + slash * half_length,
            line_width, color,
        )
        return
    base = tip + direction * size
    half = side * (size * 0.28)
    if style == "open":
        _add_box_beam(mesh, tip, base + half, line_width, color)
        _add_box_beam(mesh, tip, base - half, line_width, color)
        return
    depth = normal * (size * 0.08)
    front_tip = tip + depth
    front_left = base + half + depth
    front_right = base - half + depth
    back_tip = tip - depth
    back_left = base + half - depth
    back_right = base - half - depth
    for loop in (
        (front_tip, front_left, front_right),
        (back_tip, back_right, back_left),
        (front_tip, back_tip, back_left, front_left),
        (front_left, back_left, back_right, front_right),
        (front_right, back_right, back_tip, front_tip),
    ):
        _add_face(mesh, list(loop), color)


def _text_width(mesh: Mesh, font_family: str) -> tuple[float, float]:
    if not mesh.vertices:
        name = font_family or "the system default font"
        raise ValueError(
            f'The font "{name}" produced no 3D text geometry. '
            "Choose another font."
        )
    xs = [vertex.position.x() for vertex in mesh.vertices]
    return min(xs), max(xs)


def _add_text(mesh: Mesh, text: str, center: QVector3D,
              x_axis: QVector3D, glyph_up: QVector3D,
              face_normal: QVector3D,
              height: float, extrusion: float, font_family: str,
              color: tuple[float, float, float] | None) -> float:
    from core.text3d import build_text_mesh

    source = build_text_mesh(
        text, font_family=font_family, height=height,
        thickness=extrusion, bold=True,
    )
    low, high = _text_width(source, font_family)
    midpoint = (low + high) * 0.5
    x_axis = _unit(x_axis)
    glyph_up = _unit(glyph_up)
    depth_axis = -_unit(face_normal)

    def place(point):
        return (
            center
            + x_axis * (point.x() - midpoint)
            + depth_axis * point.y()
            + glyph_up * point.z()
        )

    for face in source.faces:
        outer = [place(point) for point in face.vertices]
        holes = [[place(point) for point in hole] for hole in face.holes]
        result = mesh.add_face(outer, hole_loops=holes or None)
        _set_face_color(result, color)
    return high - low


def _view_basis(view_name: str, camera) -> tuple[
    QVector3D, QVector3D, QVector3D
]:
    views = {
        "top": (QVector3D(0.0, 0.0, 1.0), QVector3D(0.0, 1.0, 0.0)),
        "front": (QVector3D(0.0, -1.0, 0.0), QVector3D(0.0, 0.0, 1.0)),
        "right": (QVector3D(1.0, 0.0, 0.0), QVector3D(0.0, 0.0, 1.0)),
        "left": (QVector3D(-1.0, 0.0, 0.0), QVector3D(0.0, 0.0, 1.0)),
        "back": (QVector3D(0.0, 1.0, 0.0), QVector3D(0.0, 0.0, 1.0)),
        "bottom": (QVector3D(0.0, 0.0, -1.0), QVector3D(0.0, -1.0, 0.0)),
    }
    if view_name == "camera":
        if camera is None:
            raise ValueError("The viewport camera is not available.")
        normal = -_unit(QVector3D(camera.forward()))
        up = QVector3D(camera.up_vector())
        up = up - normal * QVector3D.dotProduct(up, normal)
        if up.length() <= 1e-8:
            raise ValueError("Could not determine the viewport camera orientation.")
        up = _unit(up)
    else:
        try:
            normal, up = views[view_name]
        except KeyError as exc:
            raise ValueError(f'Unknown dimension view "{view_name}".') from exc
    right = _unit(QVector3D.crossProduct(up, normal))
    return right, up, normal


def _format_value(kind: str, value: float, settings: dict,
                  decimals: int, show_unit: bool) -> str:
    from core import units

    if kind == "edge":
        length_unit = settings["length"]
        if not show_unit and length_unit in {"ft-in", "in-frac", "ft-in-frac"}:
            length_unit = "in" if length_unit == "in-frac" else "ft"
        text = units.format_length(
            value, length_unit, decimals, show_unit=show_unit
        )
    elif kind == "face":
        area_unit = units.unit_setting("area", settings)
        if area_unit == "auto":
            area_unit = units.area_unit_for(settings["length"])
        text = units.format_value(
            value, area_unit, decimals, symbol=show_unit
        )
    else:
        volume_unit = units.unit_setting("volume", settings)
        if volume_unit == "auto":
            volume_unit = units.volume_unit_for(settings["length"])
        text = units.format_value(
            value, volume_unit, decimals, symbol=show_unit
        )
    return units.localized(text, settings)


def _add_edge_dimension(mesh: Mesh, a: QVector3D, b: QVector3D,
                        label: str, height: float, extrusion: float,
                        line_width: float, font_family: str,
                        arrow_style: str, offset: float,
                        view_basis: tuple[QVector3D, QVector3D, QVector3D],
                        show_lines: bool,
                        color: tuple[float, float, float] | None) -> None:
    right, up, normal = view_basis
    depth = (
        QVector3D.dotProduct(a, normal)
        + QVector3D.dotProduct(b, normal)
    ) * 0.5
    projected_a = a + normal * (depth - QVector3D.dotProduct(a, normal))
    projected_b = b + normal * (depth - QVector3D.dotProduct(b, normal))
    projected_direction = projected_b - projected_a
    span = (b - a).length()
    if projected_direction.length() <= 1e-8:
        direction = right
        midpoint = (projected_a + projected_b) * 0.5
        projected_a = midpoint - direction * (span * 0.5)
        projected_b = midpoint + direction * (span * 0.5)
    else:
        direction = _unit(projected_direction)
    text_up = up - direction * QVector3D.dotProduct(up, direction)
    if text_up.length() <= 1e-8:
        text_up = QVector3D.crossProduct(normal, direction)
    text_up = _unit(text_up)
    offset_direction = text_up
    dim_a = projected_a + offset_direction * offset
    dim_b = projected_b + offset_direction * offset
    if show_lines:
        _add_box_beam(mesh, a, dim_a, line_width, color)
        _add_box_beam(mesh, b, dim_b, line_width, color)

    text_mesh = Mesh()
    label_width = _add_text(
        text_mesh, label,
        (dim_a + dim_b) * 0.5 + text_up * (height * 0.12),
        direction, text_up, normal, height, extrusion, font_family, color,
    )
    span = (dim_b - dim_a).length()
    gap = label_width * 0.5 + height * 0.12
    left_end = dim_a + direction * min(gap, span * 0.48)
    right_start = dim_b - direction * min(gap, span * 0.48)
    if show_lines:
        _add_box_beam(mesh, dim_a, left_end, line_width, color)
        _add_box_beam(mesh, right_start, dim_b, line_width, color)
        arrow_size = min(height * 0.45, span * 0.16)
        _add_arrow(
            mesh, dim_a, direction, text_up, normal, arrow_size, arrow_style,
            line_width, color,
        )
        _add_arrow(
            mesh, dim_b, -direction, text_up, normal, arrow_size, arrow_style,
            line_width, color,
        )
    for face in text_mesh.faces:
        outer = face.vertices
        holes = face.holes
        result = mesh.add_face(outer, hole_loops=holes or None)
        _set_face_color(result, color)


def _add_point_label(mesh: Mesh, center: QVector3D, label: str,
                     height: float, extrusion: float, line_width: float,
                     font_family: str, offset: float,
                     view_basis: tuple[QVector3D, QVector3D, QVector3D],
                     color: tuple[float, float, float] | None,
                     show_line: bool = True,
                     label_position: QVector3D | None = None,
                     leader_start: QVector3D | None = None,
                     leader_end: QVector3D | None = None) -> None:
    right, up, normal = view_basis
    label_center = (
        label_position if label_position is not None else center + up * offset
    )
    if show_line:
        _add_box_beam(
            mesh,
            leader_start if leader_start is not None else center,
            leader_end if leader_end is not None else label_center,
            line_width,
            color,
        )
    _add_text(
        mesh, label, label_center, right, up, normal,
        height, extrusion, font_family, color,
    )


def _build_measurement_group(kind: str, references: list[dict],
                             sources: list[dict], aggregate: bool,
                             options: dict) -> Group:
    from core import units

    measured = [_measure(source, kind) for source in sources]
    value = sum(item["value"] for item in measured) if aggregate \
        else measured[0]["value"]
    unit_settings = units.model_units()
    decimals = int(options.get("decimals", unit_settings["precision"]))
    show_unit = options.get("unit_display", "system") == "system"
    label = _format_value(kind, value, unit_settings, decimals, show_unit)
    if aggregate:
        label = f"Total {label}"

    mesh = Mesh()
    height = float(options["height"])
    extrusion = float(options["extrusion"])
    offset = float(options.get("offset", height * 1.5))
    font_family = str(options.get("font", ""))
    arrow_style = str(options.get("arrow_style", "closed"))
    view_name = str(options.get("view", "top"))
    view_basis = options["view_basis"]
    show_lines = bool(options.get("show_lines", True))
    color_mode = str(options.get("color_mode", "custom"))
    color = None
    if color_mode != "layer":
        raw_color = options.get("color", COLOR)
        if not isinstance(raw_color, (list, tuple)) or len(raw_color) != 3:
            raise ValueError("The measurement color must contain three RGB values.")
        color = tuple(float(channel) for channel in raw_color)
        if any(not 0.0 <= channel <= 1.0 for channel in color):
            raise ValueError("Measurement RGB values must be between 0 and 1.")
    layer_name = str(options.get("layer", DEFAULT_LAYER))
    line_width = max(height * 0.025, 0.0005)
    if aggregate or kind != "edge":
        center = sum(
            (item["center"] for item in measured), QVector3D()
        ) / len(measured)
        label_position = None
        leader_start = None
        leader_end = None
        if kind == "solid":
            leader_start = _visible_face_center(
                [
                    face
                    for item in measured
                    for face in item.get("visible_faces", [])
                ],
                view_basis,
            )
            leader_end = leader_start + view_basis[1] * offset
            clearance = max(extrusion, height * 0.25)
            label_position = leader_end + view_basis[2] * clearance
        _add_point_label(
            mesh, center, label, height, extrusion, line_width, font_family,
            offset, view_basis, color, show_lines, label_position, leader_start,
            leader_end,
        )
    else:
        _add_edge_dimension(
            mesh, measured[0]["a"], measured[0]["b"], label,
            height, extrusion, line_width, font_family, arrow_style,
            offset, view_basis, show_lines, color,
        )

    if kind == "edge":
        unit_value = _format_value(
            kind, value, unit_settings, decimals, show_unit
        )
        noun = "Length"
    elif kind == "face":
        unit_value = _format_value(
            kind, value, unit_settings, decimals, show_unit
        )
        noun = "Area"
    else:
        unit_value = _format_value(
            kind, value, unit_settings, decimals, show_unit
        )
        noun = "Volume"
    name = f"3D {noun} {'Total' if aggregate else 'Measurement'}: {unit_value}"
    group = Group(mesh, name=name[:60])
    group.layer = None if layer_name == DEFAULT_LAYER else layer_name
    group.ext = {
        KEY: {
            "version": 1,
            "kind": kind,
            "sources": references,
            "aggregate": bool(aggregate),
            "height": height,
            "extrusion": extrusion,
            "font": font_family,
            "unit_display": "system" if show_unit else "none",
            "decimals": decimals,
            "arrow_style": arrow_style,
            "offset": offset,
            "show_lines": show_lines,
            "color_mode": color_mode,
            "color": list(color) if color is not None else None,
            "layer": layer_name,
            "view": view_name,
        }
    }
    _LIVE_LINKS[group.uid] = [source["live"] for source in sources]
    return group


def _build_callout_group(
    reference: dict, source: dict, options: dict
) -> Group:
    text = str(options["callout_text"]).strip()
    if not text:
        raise ValueError("Enter text for the 3D callout.")
    kind = reference.get("kind")
    if kind == "solid":
        points, faces = _group_world_geometry(source)
        if faces:
            anchor = _visible_face_center(faces, options["view_basis"])
        else:
            anchor, _label_center = _outside_label_position(
                points, options["view_basis"], 0.0,
                float(options["height"]), float(options["extrusion"]),
            )
    elif kind == "edge":
        a = _point(source["matrix"], source["item"].a)
        b = _point(source["matrix"], source["item"].b)
        points = [a, b]
        anchor = (a + b) * 0.5
    elif kind == "face":
        face = source["item"]
        geometry = _world_face_geometry(face, source["matrix"])
        if geometry is None:
            raise ValueError("The selected face has zero area.")
        _area, anchor, _normal = geometry
        points = [
            _point(source["matrix"], vertex.position)
            for loop in (face.loop, *face.hole_loops)
            for vertex in loop
        ]
    else:
        raise ValueError("Select edges, faces, or solid groups to create callouts.")
    right, up, normal = options["view_basis"]
    height = float(options["height"])
    extrusion = float(options["extrusion"])
    offset = float(options.get("offset", height * 1.5))
    _bounds_anchor, label_center = _outside_label_position(
        points, options["view_basis"], offset, height, extrusion
    )
    color_mode = str(options.get("color_mode", "custom"))
    color = None
    if color_mode != "layer":
        raw_color = options.get("color", COLOR)
        if not isinstance(raw_color, (list, tuple)) or len(raw_color) != 3:
            raise ValueError("The callout color must contain three RGB values.")
        color = tuple(float(channel) for channel in raw_color)
        if any(not 0.0 <= channel <= 1.0 for channel in color):
            raise ValueError("Callout RGB values must be between 0 and 1.")

    mesh = Mesh()
    line_width = max(height * 0.025, 0.0005)
    line_style = str(options.get("callout_line_style", "straight"))
    if line_style not in {
        "straight", "oblique", "convex", "concave",
        "convex-concave", "concave-convex",
    }:
        raise ValueError("The selected callout line style is not supported.")
    if options.get("show_lines", True):
        _add_callout_line(
            mesh,
            anchor,
            label_center,
            line_style,
            options["view_basis"],
            height,
            line_width,
            color,
        )
    _add_text(
        mesh, text, label_center, right, up, normal,
        height, extrusion, str(options.get("font", "")), color,
    )
    for face in mesh.faces:
        _set_face_color(face, color)

    layer_name = str(options.get("layer", DEFAULT_LAYER))
    group = Group(mesh, name=f"3D Callout: {text}"[:60])
    group.layer = None if layer_name == DEFAULT_LAYER else layer_name
    group.ext = {
        KEY: {
            "version": 1,
            "kind": "callout",
            "sources": [reference],
            "text": text,
            "height": height,
            "extrusion": extrusion,
            "font": str(options.get("font", "")),
            "offset": offset,
            "show_lines": bool(options.get("show_lines", True)),
            "line_style": line_style,
            "color_mode": color_mode,
            "color": list(color) if color is not None else None,
            "layer": layer_name,
            "view": str(options.get("view", "top")),
        }
    }
    _LIVE_LINKS[group.uid] = [source["live"]]
    return group


def _measurement_dialog(parent, count: int, scene, callout: bool = False):
    from core import units

    unit = units.model_unit()
    dialog = QDialog(parent)
    dialog.setWindowTitle("3D Callout" if callout else "3D Measurements")
    layout = QVBoxLayout(dialog)
    form = QFormLayout()
    font = QFontComboBox(dialog)
    form.addRow("Font:", font)
    callout_text = None
    if callout:
        callout_text = QLineEdit(dialog)
        callout_text.setPlaceholderText("Part name or note")
        form.addRow("Text:", callout_text)
    unit_display = QComboBox(dialog)
    unit_display.addItem("System units", "system")
    unit_display.addItem("No unit", "none")
    form.addRow("Units:", unit_display)
    decimals = QSpinBox(dialog)
    decimals.setRange(0, 6)
    decimals.setValue(units.model_precision())
    form.addRow("Decimal places:", decimals)
    arrow_style = QComboBox(dialog)
    for label, value in (
        ("Point", "point"),
        ("Open arrow", "open"),
        ("Closed arrow", "closed"),
        ("Oblique", "oblique"),
    ):
        arrow_style.addItem(label, value)
    form.addRow("Arrow style:", arrow_style)
    view = QComboBox(dialog)
    for label, value in (
        ("Top", "top"),
        ("Front", "front"),
        ("Right", "right"),
        ("Left", "left"),
        ("Back", "back"),
        ("Bottom", "bottom"),
        ("Camera", "camera"),
    ):
        view.addItem(label, value)
    form.addRow("Dimension view:", view)
    callout_line_style = QComboBox(dialog)
    for label, value in (
        ("Straight", "straight"),
        ("Oblique", "oblique"),
        ("Convex", "convex"),
        ("Concave", "concave"),
        ("Convex-concave", "convex-concave"),
        ("Concave-convex", "concave-convex"),
    ):
        callout_line_style.addItem(label, value)
    if callout:
        form.addRow("Leader line style:", callout_line_style)
    color_mode = QComboBox(dialog)
    color_mode.addItem("Custom color", "custom")
    color_mode.addItem("Layer color", "layer")
    form.addRow("Color source:", color_mode)
    color_state = [COLOR]
    color_button = QPushButton("Choose color…", dialog)

    def update_color_button() -> None:
        color = QColor.fromRgbF(*color_state[0])
        color_button.setStyleSheet(
            f"background-color: {color.name()}; color: "
            f"{'white' if color.lightness() < 128 else 'black'};"
        )

    def choose_color() -> None:
        picked = QColorDialog.getColor(
            QColor.fromRgbF(*color_state[0]), dialog, "Measurement color"
        )
        if not picked.isValid():
            return
        color_state[0] = (picked.redF(), picked.greenF(), picked.blueF())
        update_color_button()

    update_color_button()
    color_button.clicked.connect(choose_color)
    color_mode.currentIndexChanged.connect(
        lambda _index: color_button.setEnabled(
            color_mode.currentData() == "custom"
        )
    )
    form.addRow("Custom color:", color_button)
    layer = QComboBox(dialog)
    layer_names = [DEFAULT_LAYER]
    layer_names.extend(
        name for name in (
            str(getattr(item, "name", "")) for item in getattr(scene, "layers", ())
        )
        if name and name not in layer_names
    )
    for name in layer_names:
        layer.addItem(name, name)
    selected_layer = active_layer(scene)
    selected_index = layer.findData(selected_layer)
    if selected_index >= 0:
        layer.setCurrentIndex(selected_index)
    form.addRow("Layer:", layer)
    height = QDoubleSpinBox(dialog)
    height.setDecimals(4)
    height.setRange(units.convert(0.001, "m", unit), units.convert(10.0, "m", unit))
    height.setSingleStep(units.convert(0.01, "m", unit))
    height.setValue(units.convert(0.1, "m", unit))
    extrusion = QDoubleSpinBox(dialog)
    extrusion.setDecimals(4)
    extrusion.setRange(0.0, units.convert(10.0, "m", unit))
    extrusion.setSingleStep(units.convert(0.005, "m", unit))
    extrusion.setValue(units.convert(0.01, "m", unit))
    form.addRow(f"Text height ({unit}):", height)
    form.addRow(f"Text extrusion ({unit}):", extrusion)
    offset = QDoubleSpinBox(dialog)
    offset.setDecimals(4)
    offset.setRange(units.convert(-10.0, "m", unit),
                    units.convert(10.0, "m", unit))
    offset.setSingleStep(units.convert(0.01, "m", unit))
    offset.setValue(units.convert(0.15, "m", unit))
    form.addRow(f"Offset ({unit}):", offset)
    show_lines = QCheckBox(
        "Show leader line" if callout else "Show dimension and leader lines",
        dialog,
    )
    show_lines.setChecked(True)
    if callout:
        for field in (unit_display, decimals, arrow_style):
            label = form.labelForField(field)
            field.hide()
            if label is not None:
                label.hide()
    layout.addLayout(form)
    total = QCheckBox("Also create a total for the selection", dialog)
    total.setChecked(False)
    total.setEnabled(count > 1)
    if callout:
        total.hide()
    else:
        layout.addWidget(total)
    layout.addWidget(show_lines)
    buttons = QDialogButtonBox(
        QDialogButtonBox.Ok | QDialogButtonBox.Cancel, parent=dialog
    )
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)
    if not dialog.exec():
        return None
    factor = units.convert(1.0, unit, "m")
    return {
        "height": height.value() * factor,
        "extrusion": extrusion.value() * factor,
        "offset": offset.value() * factor,
        "font": font.currentFont().family(),
        "total": total.isChecked(),
        "show_lines": show_lines.isChecked(),
        "unit_display": unit_display.currentData(),
        "decimals": decimals.value(),
        "arrow_style": arrow_style.currentData(),
        "view": view.currentData(),
        "color_mode": color_mode.currentData(),
        "color": list(color_state[0]),
        "layer": layer.currentData(),
        "callout_text": (
            callout_text.text().strip() if callout_text is not None else ""
        ),
        "callout_line_style": callout_line_style.currentData(),
    }


class _ReplaceGroupsCommand(Command):
    """Undoable insertion or replacement of generated measurement groups."""

    def __init__(self, scene, replacements: list[tuple], additions: list[Group]):
        self._replacements = replacements
        self._additions = additions
        self._selection = list(scene.selection)

    @staticmethod
    def _bump(scene) -> None:
        scene.version = getattr(scene, "version", 0) + 1

    def do(self, scene) -> None:
        by_container = {}
        for parent, old, new, index in self._replacements:
            by_container.setdefault(id(parent), (parent, [], []))
            by_container[id(parent)][1].append(old)
            by_container[id(parent)][2].append((index, new))
        for parent, olds, news in by_container.values():
            for old in olds:
                if old in parent:
                    parent.remove(old)
            for index, new in sorted(news, key=lambda item: item[0]):
                parent.insert(min(index, len(parent)), new)
        scene.groups.extend(self._additions)
        scene.selection.clear()
        for group in [new for _parent, _old, new, _index in self._replacements]:
            scene.selection.add(group)
        for group in self._additions:
            scene.selection.add(group)
        self._bump(scene)

    def undo(self, scene) -> None:
        for group in self._additions:
            if group in scene.groups:
                scene.groups.remove(group)
        by_container = {}
        for parent, old, new, index in self._replacements:
            by_container.setdefault(id(parent), (parent, [], []))
            by_container[id(parent)][1].append(new)
            by_container[id(parent)][2].append((index, old))
        for parent, news, olds in by_container.values():
            for new in news:
                if new in parent:
                    parent.remove(new)
            for index, old in sorted(olds, key=lambda item: item[0]):
                parent.insert(min(index, len(parent)), old)
        scene.selection.clear()
        for item in self._selection:
            scene.selection.add(item)
        self._bump(scene)


def _notify(viewport) -> None:
    notify = getattr(viewport, "notify_scene_changed", None)
    if callable(notify):
        notify()
    viewport.update()


def _execute(viewport, command) -> None:
    viewport.history.execute(command)
    error = getattr(viewport.history, "last_error", None)
    if error:
        raise RuntimeError(error)
    _notify(viewport)


def create_measurements(app) -> None:
    viewport = app.viewport
    scene = viewport.scene
    try:
        kind, sources = _selected_sources(scene)
        options = _measurement_dialog(
            viewport.window(), len(sources), scene
        )
        if options is None:
            return
        options["view_basis"] = _view_basis(
            str(options["view"]), getattr(viewport, "camera", None)
        )
        refs = [source["reference"] for source in sources]
        resolved = [
            _resolve_reference(scene, source["reference"], source["live"])
            for source in sources
        ]
        groups = [
            _build_measurement_group(
                kind, [source["reference"]], [source], False, options
            )
            for source in resolved
        ]
        if options["total"] and len(sources) > 1:
            groups.append(_build_measurement_group(
                kind, refs, resolved, True, options
            ))
        _execute(viewport, _ReplaceGroupsCommand(scene, [], groups))
    except Exception as exc:
        log.exception("Could not create 3D measurements")
        QMessageBox.critical(None, "3D Measurements", str(exc))


def create_callouts(app) -> None:
    viewport = app.viewport
    scene = viewport.scene
    try:
        _kind, sources = _selected_sources(scene)
        options = _measurement_dialog(
            viewport.window(), len(sources), scene, callout=True
        )
        if options is None:
            return
        if not options["callout_text"]:
            raise ValueError("Enter text for the 3D callout.")
        options["view_basis"] = _view_basis(
            str(options["view"]), getattr(viewport, "camera", None)
        )
        references = [source["reference"] for source in sources]
        resolved = [
            _resolve_reference(scene, source["reference"], source["live"])
            for source in sources
        ]
        callouts = [
            _build_callout_group(reference, source, options)
            for reference, source in zip(references, resolved)
        ]
        _execute(viewport, _ReplaceGroupsCommand(scene, [], callouts))
    except Exception as exc:
        log.exception("Could not create 3D callouts")
        QMessageBox.critical(None, "3D Callouts", str(exc))


def edit_callout_text(app) -> None:
    viewport = app.viewport
    scene = viewport.scene
    try:
        selected = [
            item for item in scene.selection
            if isinstance(item, Group)
            and isinstance((item.ext or {}).get(KEY), dict)
            and (item.ext or {})[KEY].get("kind") == "callout"
        ]
        if len(selected) != 1:
            raise ValueError("Select exactly one generated 3D callout group.")
        group = selected[0]
        data = (group.ext or {})[KEY]
        text, accepted = QInputDialog.getText(
            viewport.window(),
            "Edit 3D Callout",
            "Text:",
            QLineEdit.Normal,
            str(data.get("text", "")),
        )
        if not accepted:
            return
        if not text.strip():
            raise ValueError("Callout text cannot be empty.")
        references = data.get("sources")
        if not isinstance(references, list) or len(references) != 1:
            raise ValueError("The selected callout has invalid source data.")
        live_sources = _LIVE_LINKS.get(group.uid, [])
        source = _resolve_reference(
            scene,
            references[0],
            live_sources[0] if live_sources else None,
        )
        view_name = str(data.get("view", "top"))
        options = {
            "callout_text": text.strip(),
            "height": float(data["height"]),
            "extrusion": float(data["extrusion"]),
            "font": str(data.get("font", "")),
            "offset": float(data.get("offset", float(data["height"]) * 1.5)),
            "show_lines": bool(data.get("show_lines", True)),
            "callout_line_style": str(data.get("line_style", "straight")),
            "color_mode": str(data.get("color_mode", "custom")),
            "color": data.get("color", COLOR),
            "layer": str(data.get("layer", group.layer or DEFAULT_LAYER)),
            "view": view_name,
            "view_basis": _view_basis(
                view_name, getattr(viewport, "camera", None)
            ),
        }
        updated = _build_callout_group(references[0], source, options)
        location = next(
            (
                (parent, index)
                for candidate, parent, index, _path in _group_locations(scene)
                if candidate is group
            ),
            None,
        )
        if location is None:
            raise ValueError("Could not locate the selected callout in the document.")
        parent, index = location
        _execute(
            viewport,
            _ReplaceGroupsCommand(scene, [(parent, group, updated, index)], []),
        )
    except Exception as exc:
        log.exception("Could not edit 3D callout text")
        QMessageBox.critical(None, "Edit 3D Callout", str(exc))


def update_measurements(app) -> None:
    viewport = app.viewport
    scene = viewport.scene
    try:
        replacements = []
        for group, parent, index, _path in _group_locations(scene):
            data = (group.ext or {}).get(KEY)
            if not isinstance(data, dict) or data.get("version") != 1:
                continue
            kind = data.get("kind")
            refs = data.get("sources")
            if kind not in {"edge", "face", "solid", "callout"} \
                    or not isinstance(refs, list):
                raise ValueError("A 3D measurement has invalid source data.")
            layer_name = str(data.get("layer", group.layer or DEFAULT_LAYER))
            if layer_name != DEFAULT_LAYER and not any(
                getattr(layer, "name", None) == layer_name
                for layer in getattr(scene, "layers", ())
            ):
                raise ValueError(
                    f'The layer "{layer_name}" for a 3D measurement no longer exists.'
                )
            live_sources = _LIVE_LINKS.get(group.uid, [])
            sources = [
                _resolve_reference(
                    scene, reference,
                    live_sources[index] if index < len(live_sources) else None,
                )
                for index, reference in enumerate(refs)
            ]
            view_name = str(data.get("view", "top"))
            if "view" not in data:
                legacy_axis = data.get("text_height_axis")
                if (
                    data.get("text_orientation") == "vertical"
                    or legacy_axis == "Z"
                ):
                    view_name = "front"
                elif legacy_axis == "X":
                    view_name = "bottom"
            options = {
                "height": float(data["height"]),
                "extrusion": float(data["extrusion"]),
                "offset": float(data.get("offset", float(data["height"]) * 1.5)),
                "font": str(data.get("font", "")),
                "show_lines": bool(data.get("show_lines", True)),
                "unit_display": str(data.get("unit_display", "system")),
                "decimals": int(data.get("decimals", 2)),
                "arrow_style": str(data.get("arrow_style", "closed")),
                "color_mode": str(data.get("color_mode", "custom")),
                "color": data.get("color", COLOR),
                "layer": layer_name,
                "view": view_name,
                "view_basis": _view_basis(
                    view_name, getattr(viewport, "camera", None)
                ),
            }
            if kind == "callout":
                if len(refs) != 1 or not isinstance(data.get("text"), str):
                    raise ValueError("A 3D callout has invalid source or text data.")
                options["callout_text"] = data["text"]
                options["callout_line_style"] = str(
                    data.get("line_style", "straight")
                )
                updated = _build_callout_group(refs[0], sources[0], options)
            else:
                updated = _build_measurement_group(
                    kind, refs, sources, bool(data.get("aggregate")), options
                )
            replacements.append((parent, group, updated, index))
        if not replacements:
            raise ValueError("No 3D measurement groups were found in this document.")
        _execute(viewport, _ReplaceGroupsCommand(scene, replacements, []))
    except Exception as exc:
        log.exception("Could not update 3D measurements")
        QMessageBox.critical(None, "Update 3D Measurements", str(exc))


def setup(app) -> None:
    app.add_menu_action(
        "Create 3D Measurements…",
        lambda: create_measurements(app),
        tip=(
            "Create GLB-exportable 3D length, area, and volume measurements "
            "from the current selection."
        ),
    )
    app.add_menu_action(
        "Create 3D Callout…",
        lambda: create_callouts(app),
        tip="Create editable, GLB-exportable text callouts for selected solid groups.",
    )
    app.add_menu_action(
        "Edit Selected 3D Callout Text…",
        lambda: edit_callout_text(app),
        tip="Change the text of the selected generated 3D callout.",
    )
    app.add_menu_action(
        "Update 3D Measurements",
        lambda: update_measurements(app),
        tip="Refresh generated 3D measurements after changing the source geometry.",
    )
    log.info("3D Measurements extension loaded (key=%s)", getattr(app, "key", "?"))
