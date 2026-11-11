"""从实际源码和实际 SQLite schema 生成九部分交付稿与干净发行包。"""
from __future__ import annotations

import hashlib
import sys
import zipfile
from pathlib import Path
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
from growing_companion.core.db import Database  # noqa: E402


def code_section(files: list[Path]) -> str:
    parts: list[str] = []
    for path in files:
        language = {'.py': 'python', '.json': 'json', '.yaml': 'yaml'}.get(path.suffix, 'text')
        content = path.read_text(encoding='utf-8').strip()
        label = path.relative_to(ROOT).as_posix()
        parts.append(f'### `{label}`\n\n' + (f'```{language}\n{content}\n```' if content else '包标识文件，故意为空。'))
    return '\n\n'.join(parts)


def build() -> None:
    db = Database(':memory:')
    try:
        rows = db.query("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' ORDER BY type DESC,name")
        schema = 'PRAGMA foreign_keys=ON;\n\n' + ';\n\n'.join(r['sql'] for r in rows) + ';\n\nPRAGMA user_version=3;\n'
    finally:
        db.close()
    (ROOT / 'docs/schema_v3.sql').write_text(schema, encoding='utf-8')
    report = ROOT / 'docs/test-results.xml'
    if report.exists():
        tree = ET.parse(report)
        suites = tree.getroot().findall('testsuite')
        counts = {key: sum(int(s.attrib.get(key, '0')) for s in suites) for key in ('tests', 'failures', 'errors', 'skipped')}
        result = (f"本次本机测试：**{counts['tests']} 项，失败 {counts['failures']}，错误 {counts['errors']}，跳过 {counts['skipped']}**。"
                  '使用 Python 3.12；AstrBot 接口使用离线桩，3D 使用真实 Three.js r128 + Chromium。'
                  '详细机器可读记录见 `docs/test-results.xml`。')
    else:
        result = '测试结果尚未生成；请先运行 pytest --junitxml=docs/test-results.xml。'
    sources = [ROOT / 'main.py', ROOT / '__init__.py'] + sorted((ROOT / 'core').rglob('*.py'))
    tests = sorted((ROOT / 'tests').glob('*.py'))
    template = (ROOT / 'docs/DELIVERY_TEMPLATE.md').read_text(encoding='utf-8')
    for marker, value in {'CONFIG': code_section([ROOT / 'metadata.yaml', ROOT / '_conf_schema.json', ROOT / 'requirements.txt']),
                          'SQL': f'```sql\n{schema}\n```', 'SOURCE': code_section(sources),
                          'TESTS': code_section(tests), 'RESULT': result}.items():
        template = template.replace('{{' + marker + '}}', value)
    (ROOT / 'DELIVERY_V3.md').write_text(template, encoding='utf-8')
    files = sources + tests + [ROOT / name for name in ('metadata.yaml', '_conf_schema.json', 'requirements.txt', 'README.md', 'DELIVERY_V3.md')]
    files += sorted((ROOT / 'docs').glob('*')) + sorted((ROOT / 'tools').glob('*.py'))
    files += sorted((ROOT / 'core/brainmap/vendor').glob('*'))
    archive = ROOT.parent / 'growing_companion_v0.3.0.zip'
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as bundle:
        for file in sorted(set(files)):
            if file.is_file():
                bundle.write(file, (Path(ROOT.name) / file.relative_to(ROOT)).as_posix())
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix('.zip.sha256').write_text(digest + '  ' + archive.name + '\n', encoding='ascii')
    print(f'Created {archive.name}: {archive.stat().st_size} bytes; {len(set(files))} files')


if __name__ == '__main__':
    # 保留旧构建器代码供历史参考；当前入口统一生成 4.0，避免覆盖旧版发行包。
    from build_delivery_v4 import build
    build()
