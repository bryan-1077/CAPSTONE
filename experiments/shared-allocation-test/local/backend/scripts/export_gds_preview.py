import os
import sys

import pya


def _arg(name, default=None):
    value = globals().get(name)
    if value is None:
        return default
    return value


input_path = _arg("input")
output_path = _arg("output", "gds_preview.png")
size = int(_arg("size", "2400"))

if not input_path:
    print("Missing -rd input=<layout.gds>", file=sys.stderr)
    sys.exit(2)

input_path = os.path.abspath(input_path)
output_path = os.path.abspath(output_path)

app = pya.Application.instance()
mw = app.main_window()
mw.load_layout(input_path, 0)

view = mw.current_view()
if view is None:
    print("KLayout did not create a layout view", file=sys.stderr)
    sys.exit(1)

view.max_hier()
view.zoom_fit()
view.save_image(output_path, size, size)
print(output_path)
