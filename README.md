[README_dimensions_3d.md](https://github.com/user-attachments/files/34967174/README_dimensions_3d.md)
# 3D Measurements for IngeTrazo

This extension turns selected geometry measurements into actual 3D mesh
geometry, so the dimension lines and text can be exported and viewed in GLB
files.

<img width="648" height="720" alt="dim" src="https://github.com/user-attachments/assets/e7e001af-783c-49e2-84da-071591c44784" />


## Features

- Select edges to create an individual length dimension for each edge.
- Select faces to create an individual area label for each face. Face holes are
  excluded from the measured area.
- Select solid groups to create an individual volume label for each group.
  Solids must be groups to be selectable for measurement. Only closed meshes
  are measured; nested closed meshes are included.
- Create 3D callouts with editable text for naming selected edges, faces, or
  solid groups.
  Callout text can be changed later by selecting the callout group and choosing
  **Extensions > Edit Selected 3D Callout Text…**.
- Optionally add a total for the selected edges, faces, or solid groups.
- Set the 3D text height and extrusion depth when creating measurements.
- Display values in the document's system units or without visible unit
  symbols, and choose 0–6 decimal places.
- Choose point, open arrow, closed arrow, or oblique dimension markers.
- Use a custom color or the selected layer's color, and assign the generated
  groups to a layer.
- Set a positive or negative offset between the measured geometry and its
  dimension lines/text.
- Hide dimension and leader lines while keeping the 3D labels visible, or keep
  the lines enabled.
- Orient dimensions to the current camera or the Top, Front, Right, Left, Back,
  or Bottom view.
- Update generated measurements from **Extensions > Update 3D Measurements**
  after editing the source geometry.
- Creation and updates can be undone with `Ctrl+Z`.

The generated objects are named IngeTrazo groups containing face-based
dimension lines and real 3D text. Their color comes from the chosen custom
color or the selected layer. Export the model as GLB using IngeTrazo's normal
export command.

## Installation

Copy `dimensions_3d.py` to the IngeTrazo user plugins folder:

```text
%APPDATA%\ingetrazo\plugins\
```

Restart IngeTrazo. The **Create 3D Measurements…**, **Create 3D Callout…**,
**Edit Selected 3D Callout Text…**, and **Update 3D Measurements** commands
will appear in the **Extensions** menu.

## Usage

1. Select edges, faces, or solid groups of the same type in the viewport.
   Solids must be groups to be selected and measured.
2. Choose **Extensions > Create 3D Measurements…**.
3. Choose a font, units display, decimal precision, arrow style, dimension
   view, color source, and layer. Set text height, extrusion depth, and offset.
   Clear **Show dimension and leader lines** to create text-only labels, or
   enable **Also create a total for the selection** if needed.
4. After changing the source geometry, choose
   **Extensions > Update 3D Measurements**.
5. Export the model as GLB using IngeTrazo's normal export command.

To add a named callout, select one or more edges, faces, or solid groups of the
same type and choose
**Extensions > Create 3D Callout…**. Enter the text and choose its view, color,
layer, text size, extrusion, offset, and leader style (straight, oblique,
convex, concave, convex-concave, or concave-convex). Clear **Show leader line**
to create text without the callout line. To change the text later, select the
generated callout group and choose **Extensions > Edit Selected 3D Callout Text…**.

## Updating measurements

The update command refreshes every generated measurement and callout group in
the open document. Source references are stored with the groups. When a source
mesh's topology has changed and the original edge or face can no longer be
identified safely, the extension reports that the measurement must be
recreated from the current selection instead of silently measuring a different
element.

The selected units display, decimal precision, arrow style, offset, dimension
view, color, and layer are saved with each generated measurement and reused by
the update command. **System units** uses the document's current length, area,
and volume units. **No unit** omits unit symbols from the labels. For compound
imperial length formats, values without unit symbols are shown as decimal feet
or inches.

**Camera** uses the viewport camera when creating or updating measurements.
The named views use IngeTrazo's Z-up convention. **Layer color** uses the
chosen layer's color, and the generated groups are assigned to that layer.

Volume totals add the volumes of the selected closed meshes; they do not
calculate the Boolean union of overlapping solids.

## Limitations

- Select one geometry type at a time: edges, faces, or solid groups.
- If an edge or face is shared by multiple component placements, make the
  component unique or edit it before measuring.
- Generated text is mesh geometry, not a native text object. Callout wording
  can be changed with **Edit Selected 3D Callout Text…**. If a font has no
  usable glyph outlines, choose another font in the dialog.
