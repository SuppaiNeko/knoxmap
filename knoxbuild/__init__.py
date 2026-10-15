"""Generate furnished Project Zomboid buildings from Knoxify footprints."""
from PIL import Image as _Image

# Pillow refuses images past ~179 Mpx as a decompression-bomb guard. The
# bitmaps read here are KnoxMap's own output, and web.checks._too_big_for_memory
# already guards how big a map may get, so the guard only blocks big maps.
_Image.MAX_IMAGE_PIXELS = None
