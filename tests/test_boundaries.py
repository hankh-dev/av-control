"""관제 센터는 차량이 보고한 정보만 안다: center 코드는 시뮬레이터 쪽을 import하지 않는다."""

import ast
from pathlib import Path

CENTER = Path(__file__).parents[1] / "src" / "av_control" / "center"
FORBIDDEN = ("traci", "sumolib", "libsumo", "sumo", "av_control.bridge")


def _imports(path: Path) -> list[str]:
    names = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


def test_center_does_not_import_simulator():
    files = list(CENTER.rglob("*.py"))
    assert files
    violations = [
        f"{path.name}: {name}"
        for path in files
        for name in _imports(path)
        if any(name == bad or name.startswith(bad + ".") for bad in FORBIDDEN)
    ]
    assert violations == []
