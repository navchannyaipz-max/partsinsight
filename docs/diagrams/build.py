import pathlib, subprocess, sys
sys.path.insert(0, str(pathlib.Path(__file__).parent))
from common import HEAD
here = pathlib.Path(__file__).parent; out = here / "png"; out.mkdir(exist_ok=True)
for f in sorted((here / "src").glob("*.dot")):
    txt = f.read_text(encoding="utf-8").replace("GRAPH_HEAD", HEAD)
    tmp = out / (f.stem + ".dot"); tmp.write_text(txt, encoding="utf-8")
    subprocess.run(["dot", "-Tpng", "-Gdpi=220", str(tmp), "-o", str(out / (f.stem + ".png"))], check=True)
    tmp.unlink(); print(f.stem)
