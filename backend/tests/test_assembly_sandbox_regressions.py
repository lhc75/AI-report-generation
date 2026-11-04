"""插入装配完整性及 macOS/Windows 隔离真实执行回归；不连接业务数据库或模型。"""
import base64
import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
from docx import Document
from docx.enum.section import WD_SECTION
from docx.oxml.ns import qn

from app import assemble, sandbox

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
)
MAC_ONLY = pytest.mark.skipif(sys.platform != "darwin", reason="真实 Seatbelt 回归仅适用 Mac")
WIN_ONLY = pytest.mark.skipif(sys.platform != "win32", reason="真实作业对象回归仅适用 Windows")


def node(workdir, identifier, kind="text", content=None, anchor=None, sort=None):
    contents = workdir / "contents"
    contents.mkdir(exist_ok=True)
    suffix = {"text": "txt", "table": "json", "image": "png"}[kind]
    path = contents / f"t{identifier}.{suffix}"
    if kind == "image":
        path.write_bytes(PNG)
    elif kind == "table":
        path.write_text(json.dumps(content or {"columns": ["组别", "人数"], "rows": [["A", 20]]}, ensure_ascii=False), encoding="utf-8")
    else:
        path.write_text(content or f"模块{identifier}正文", encoding="utf-8")
    return {
        "id": identifier, "type": kind, "title": f"模块{identifier}", "level": 2,
        "instruction": "【填写：原模块】", "slot_key": f"slot-{identifier}",
        "anchor_task_id": anchor, "sort": sort if sort is not None else identifier,
        "file": str(path.relative_to(workdir)),
    }


def build(workdir, nodes, *, section_break=False):
    (workdir / "out").mkdir(exist_ok=True)
    doc = Document()
    doc.add_heading("原章节", level=1)
    doc.add_paragraph("原有正文必须保留")
    anchor = doc.add_paragraph("【填写：原模块】")
    if section_break:
        doc.add_section(WD_SECTION.NEW_PAGE)
        section_p = doc.paragraphs[-1]._p
        properties = section_p.pPr.find(qn("w:sectPr"))
        section_p.pPr.remove(properties)
        anchor._p.get_or_add_pPr().append(properties)
        section_p.getparent().remove(section_p)
    doc.add_paragraph("原模块后面的原正文")
    template = workdir / "template.docx"
    doc.save(template)
    manifest = {"template": "template.docx", "signature": assemble._doc_signature(doc), "nodes": nodes}
    output = workdir / "out" / "report.docx"
    assemble.assemble_template_docx(template, manifest, output)
    return Document(output), manifest


@pytest.mark.parametrize("anchor_kind", ["text", "table", "image"])
def test_insertions_never_overwrite_anchor_and_keep_sibling_chain_order(tmp_path, anchor_kind):
    first = node(tmp_path, 1, anchor_kind, "原模块第一行\n原模块第二行" if anchor_kind == "text" else None)
    # 刻意乱序：组装应按锚点依赖及同锚点 sort，而不是偶然的 manifest 顺序。
    children = [node(tmp_path, 4, anchor=1, sort=20), node(tmp_path, 3, anchor=2), node(tmp_path, 2, anchor=1, sort=10)]
    doc, manifest = build(tmp_path, [first, *children])
    texts = [paragraph.text for paragraph in doc.paragraphs]
    assert texts.index("模块2正文") < texts.index("模块3正文") < texts.index("模块4正文")
    assert texts.index("模块4正文") < texts.index("原模块后面的原正文")
    if anchor_kind == "text":
        assert texts.index("原模块第二行") < texts.index("模块2正文")
        assert "原模块第一行" in texts
    elif anchor_kind == "table":
        assert len(doc.tables) == 1
        assert doc.tables[0].cell(1, 1).text == "20"
        assert doc.element.body.index(doc.tables[0]._tbl) < doc.element.body.index(doc.paragraphs[texts.index("模块2正文")]._p)
    else:
        assert len(doc.inline_shapes) == 1
    assert assemble.make_report_validator(manifest)("ok", tmp_path)[0]


def test_no_section_properties_are_copied_to_generated_paragraphs(tmp_path):
    doc, manifest = build(tmp_path, [
        node(tmp_path, 1, content="第一行\n第二行"), node(tmp_path, 2, anchor=1), node(tmp_path, 3, anchor=1),
    ], section_break=True)
    assert len(doc.sections) == manifest["signature"]["sections"] == 2
    assert len(list(doc.element.iter(qn("w:sectPr")))) == 2
    inserted = next(paragraph for paragraph in doc.paragraphs if paragraph.text == "模块3正文")
    assert inserted._p.pPr.find(qn("w:sectPr")) is not None


@pytest.mark.parametrize("invalid", ["missing", "cycle", "heading"])
def test_invalid_anchor_graph_is_rejected(tmp_path, invalid):
    nodes = [node(tmp_path, 1)]
    if invalid == "missing":
        nodes.append(node(tmp_path, 2, anchor=999))
    elif invalid == "cycle":
        nodes.extend([node(tmp_path, 2, anchor=3), node(tmp_path, 3, anchor=2)])
    else:
        nodes.extend([{"id": 9, "type": "heading", "title": "原章节", "level": 1}, node(tmp_path, 2, anchor=9)])
    with pytest.raises(assemble.TemplateAssemblyError, match="锚点"):
        build(tmp_path, nodes)


def test_validator_detects_lost_text_even_if_headings_and_structure_survive(tmp_path):
    doc, manifest = build(tmp_path, [node(tmp_path, 1, content="原模块正文"), node(tmp_path, 2, anchor=1)])
    next(paragraph for paragraph in doc.paragraphs if paragraph.text == "原模块正文").text = "覆盖后的错误正文"
    doc.save(tmp_path / "out" / "report.docx")
    ok, reason, _ = assemble.make_report_validator(manifest)("ok", tmp_path)
    assert not ok and "正文丢失" in reason


def test_validator_requires_each_duplicate_module_occurrence(tmp_path):
    doc, manifest = build(tmp_path, [node(tmp_path, 1, content="相同正文"), node(tmp_path, 2, content="相同正文", anchor=1)])
    repeated = [paragraph for paragraph in doc.paragraphs if paragraph.text == "相同正文"]
    assert len(repeated) == 2
    repeated[1]._p.getparent().remove(repeated[1]._p)
    doc.save(tmp_path / "out" / "report.docx")
    assert not assemble.make_report_validator(manifest)("ok", tmp_path)[0]


def test_workdirs_are_unique_and_never_remove_previous_run():
    first = sandbox.fresh_workdir(987654, 9, "same-token")
    sentinel = first / "data" / "sentinel.txt"
    sentinel.write_text("keep")
    second = sandbox.fresh_workdir(987654, 9, "same-token")
    assert first != second and sentinel.read_text() == "keep"


def test_output_path_rejects_traversal_symlinks_and_hardlinks(tmp_path):
    workdir = tmp_path / "work"
    workdir.mkdir()
    outside = tmp_path / "private.txt"
    outside.write_text("private")
    try:
        (workdir / "symlink").symlink_to(outside)
        os.link(outside, workdir / "hardlink")
    except OSError:
        pytest.skip("当前 Windows 会话没有创建符号链接/硬链接的权限")
    for relative in ("../private.txt", "symlink", "hardlink"):
        with pytest.raises(ValueError):
            sandbox.output_path(workdir, relative)
    assert sandbox.output_path(workdir, "allowed.txt") == workdir / "allowed.txt"


def test_unavailable_sandbox_refuses_execution(tmp_path, monkeypatch):
    if sys.platform == "darwin":
        monkeypatch.setattr(sandbox, "SANDBOX_EXEC", tmp_path / "missing")
    else:
        # 强制 macOS 后端，非 darwin 平台必须拒绝执行而不是回退普通子进程
        from app import config

        monkeypatch.setattr(config.settings, "SCRIPT_SANDBOX_BACKEND", "macos")
    result = sandbox.run_python("raise RuntimeError('must not run')", tmp_path)
    assert not result["ok"] and "不可用" in result["stderr"]


def test_failed_selfcheck_refuses_user_code(tmp_path, monkeypatch):
    def fail(*args):
        raise RuntimeError("probe failed")
    check = "_self_check" if sys.platform == "darwin" else "_windows_self_check"
    monkeypatch.setattr(sandbox, check, fail)
    result = sandbox.run_python("print('must not run')", tmp_path)
    assert not result["ok"] and "probe failed" in result["stderr"] and not result["stdout"]


@MAC_ONLY
def test_sandbox_reads_excel_and_draws_chinese_chart(tmp_path):
    import pandas as pd

    pd.DataFrame({"选项": ["甲", "乙"], "人数": [20, 30]}).to_excel(tmp_path / "source.xlsx", index=False)
    result = sandbox.run_python(
        "import pandas as pd, matplotlib.pyplot as plt\n"
        "from matplotlib.font_manager import FontProperties\n"
        "df = pd.read_excel('source.xlsx')\n"
        "assert int(df['人数'].sum()) == 50\n"
        "plt.bar([0,1], df['人数'])\n"
        "plt.title('中文统计图', fontproperties=FontProperties(fname='/System/Library/Fonts/STHeiti Medium.ttc'))\n"
        "plt.savefig('out/chart.png')\nprint('EXCEL_CHINESE_OK')", tmp_path,
    )
    assert result["ok"], result["stderr"]
    assert "EXCEL_CHINESE_OK" in result["stdout"]
    assert (tmp_path / "out" / "chart.png").stat().st_size > 1000

@WIN_ONLY
def test_windows_sandbox_executes_and_blocks_child_spawn(tmp_path):
    result = sandbox.run_python(
        "import subprocess, sys\n"
        "from pathlib import Path\n"
        "Path('out/chart.txt').write_text('data', encoding='utf-8')\n"
        "try:\n"
        "    subprocess.run([sys.executable, '-c', 'pass'], capture_output=True, timeout=5)\n"
        "    print('SPAWN_ALLOWED')\n"
        "except OSError:\n"
        "    print('SPAWN_BLOCKED')\n", tmp_path,
    )
    assert result["ok"], result["stderr"]
    assert "SPAWN_BLOCKED" in result["stdout"] and "SPAWN_ALLOWED" not in result["stdout"]
    assert (tmp_path / "out" / "chart.txt").read_text(encoding="utf-8") == "data"


@WIN_ONLY
def test_windows_sandbox_kills_timeout(tmp_path):
    result = sandbox.run_python("import time; time.sleep(30)", tmp_path, timeout=2, cpu_seconds=60)
    assert not result["ok"] and result["timeout"] and "超时" in result["stderr"]



@MAC_ONLY
def test_sandbox_denies_outside_reads_writes_symlinks_and_network(tmp_path):
    workdir = tmp_path / "work"
    workdir.mkdir()
    outside = tmp_path / "private.txt"
    outside.write_text("private")
    (workdir / "link").symlink_to(outside)
    result = sandbox.run_python(
        "import pathlib, socket, errno\n"
        f"paths = [pathlib.Path({str(outside)!r}), pathlib.Path('link')]\n"
        "for path in paths:\n"
        "    try: path.read_text()\n"
        "    except PermissionError: pass\n"
        "    else: raise AssertionError('read allowed')\n"
        "try: paths[0].write_text('changed')\n"
        "except PermissionError: pass\n"
        "else: raise AssertionError('write allowed')\n"
        "try:\n"
        "    sock = socket.socket(); sock.settimeout(1)\n"
        "    assert sock.connect_ex(('127.0.0.1', 9)) in (errno.EPERM, errno.EACCES)\n"
        "    sock.close()\n"
        "except PermissionError: pass\n"
        "print('ISOLATED')", workdir,
    )
    assert result["ok"], result["stderr"]
    assert "ISOLATED" in result["stdout"] and outside.read_text() == "private"


@MAC_ONLY
def test_read_whitelist_blocks_unlisted_system_paths(tmp_path):
    result = sandbox.run_python(
        "from pathlib import Path\n"
        "for directory in ('/usr/local', '/Applications'):\n"
        "    try: list(Path(directory).iterdir())\n"
        "    except PermissionError: pass\n"
        "    else: raise AssertionError('unlisted directory readable')\n"
        "for filename in ('/usr/bin/true', '/dev/zero'):\n"
        "    try:\n"
        "        with open(filename, 'rb') as f: f.read(1)\n"
        "    except PermissionError: pass\n"
        "    else: raise AssertionError('unlisted file readable')\n"
        "print('READ_WHITELIST_OK')", tmp_path,
    )
    assert result["ok"], result["stderr"]
    assert "READ_WHITELIST_OK" in result["stdout"]


@MAC_ONLY
def test_runtime_whitelist_excludes_backend_source(tmp_path):
    source = Path(sandbox.__file__).resolve()
    result = sandbox.run_python(
        "from pathlib import Path\n"
        f"try: Path({str(source)!r}).read_bytes()\n"
        "except PermissionError: print('SOURCE_DENIED')\n"
        "else: raise AssertionError('business source readable')", tmp_path,
    )
    assert result["ok"] and "SOURCE_DENIED" in result["stdout"], result["stderr"]


@MAC_ONLY
def test_configured_log_and_file_limits_are_enforced(tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "SCRIPT_SANDBOX_MAX_LOG_BYTES", 8192)
    monkeypatch.setattr(settings, "SCRIPT_SANDBOX_MAX_OUTPUT_BYTES", 4096)
    result = sandbox.run_python("print('x' * 20000)", tmp_path)
    assert not result["ok"] and result["output_limit"]
    assert (tmp_path / "execution.stdout.log").stat().st_size == 8192
    result = sandbox.run_python("open('out/large.bin', 'wb').write(b'x' * 20000)", tmp_path)
    assert not result["ok"]
    assert (tmp_path / "out" / "large.bin").stat().st_size <= 4096


@MAC_ONLY
def test_log_volume_is_bounded(tmp_path):
    result = sandbox.run_python("import os\nwhile True: os.write(1, b'x' * 10000)", tmp_path, timeout=3)
    assert not result["ok"] and result["output_limit"]
    assert len(result["stdout"].encode()) <= sandbox.MAX_RESULT_BYTES
    assert (tmp_path / "execution.stdout.log").stat().st_size <= sandbox.MAX_LOG_BYTES


@MAC_ONLY
def test_timeout_and_memory_limits(tmp_path):
    timed = sandbox.run_python("import time; time.sleep(20)", tmp_path, timeout=1)
    assert not timed["ok"] and timed["timeout"]
    memory = sandbox.run_python("import time; data = bytearray(96 * 1024 * 1024); time.sleep(5)", tmp_path, mem_mb=48)
    assert not memory["ok"] and memory["memory_limit"], memory["stderr"]


@MAC_ONLY
def test_cpu_limit_stops_busy_loop(tmp_path):
    result = sandbox.run_python("while True: pass", tmp_path, timeout=5, cpu_seconds=1)
    assert not result["ok"] and not result["timeout"] and result["returncode"] != 0


@MAC_ONLY
def test_code_cannot_spawn_a_child_outside_the_managed_process_group(tmp_path):
    result = sandbox.run_python(
        "import os, sys, subprocess\n"
        "for spawn in (lambda: os.posix_spawn(sys.executable, [sys.executable, '-c', 'pass'], dict(os.environ)),\n"
        "              lambda: subprocess.Popen([sys.executable, '-c', 'pass'], start_new_session=True)):\n"
        "    try: spawn()\n"
        "    except PermissionError: pass\n"
        "    else: raise AssertionError('child allowed')\n"
        "print('CHILDREN_DENIED')", tmp_path,
    )
    assert result["ok"] and "CHILDREN_DENIED" in result["stdout"], result["stderr"]


@MAC_ONLY
def test_threaded_runs_apply_trusted_limits_before_generated_code(tmp_path):
    async def exercise():
        calls = []
        for index in range(4):
            workdir = tmp_path / f"run-{index}"
            workdir.mkdir()
            # 导入路径投毒不得替换标准库中的 resource 或启动限额设置。
            (workdir / "resource.py").write_text("raise RuntimeError('untrusted resource imported')")
            (workdir / "sitecustomize.py").write_text("raise RuntimeError('untrusted startup imported')")
            calls.append(asyncio.to_thread(sandbox.run_python,
                "import resource\n"
                "assert resource.getrlimit(resource.RLIMIT_CPU) == (2, 2)\n"
                "assert resource.getrlimit(resource.RLIMIT_FSIZE) == (10485760, 10485760)\n"
                "try: resource.setrlimit(resource.RLIMIT_CPU, (100, 100))\n"
                "except (ValueError, PermissionError): pass\n"
                "else: raise AssertionError('hard limit increased')\n"
                "print('TRUSTED_LIMITS_OK')", workdir, cpu_seconds=2))
        return await asyncio.gather(*calls)
    results = asyncio.run(exercise())
    assert all(result["ok"] and "TRUSTED_LIMITS_OK" in result["stdout"] for result in results), results


@MAC_ONLY
def test_previous_generated_code_cannot_poison_parent_script_write(tmp_path):
    workdir = tmp_path / "work"
    workdir.mkdir()
    outside = tmp_path / "private.txt"
    outside.write_text("keep")
    first = sandbox.run_python(
        "from pathlib import Path\nPath('main.py').unlink()\n"
        f"Path('main.py').symlink_to({str(outside)!r})\n", workdir,
    )
    assert first["ok"], first["stderr"]
    second = sandbox.run_python("print('must not execute')", workdir)
    assert not second["ok"] and outside.read_text() == "keep"
