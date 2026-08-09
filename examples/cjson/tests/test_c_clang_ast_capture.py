"""Shared in-memory Clang AST capture for signature + call audits.

Pure dump-count / purity tests always run when a C compiler is present.
Live packages use real compile DBs.

Run: uv run python -m pytest examples/cjson/tests/test_c_clang_ast_capture.py -q
"""
from __future__ import annotations

import json
import sys
from copy import deepcopy
from itertools import product
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[3]
sys.path.insert(0, str(ROOT / "scripts"))

import c_clang_ast_capture as cap_mod  # type: ignore
from c_clang_ast_audit import (  # type: ignore
    audit_to_json as function_audit_to_json,
    build_function_audit_from_capture,
    run_clang_ast_audit,
)
from c_clang_ast_capture import (  # type: ignore
    ClangAstCaptureError,
    assert_function_and_call_reports_agree,
    capture_clang_ast_package,
)
from c_clang_call_audit import (  # type: ignore
    audit_to_json as call_audit_to_json,
    build_call_audit_from_capture,
    run_clang_call_audit,
)
from c_clang_calls import FACT_KIND as CALL_FACT_KIND  # type: ignore
from c_clang_signatures import FACT_KIND as SIG_FACT_KIND  # type: ignore
from c_preprocessor import find_c_compiler  # type: ignore
from extract_c import build_c_byog  # type: ignore
from index_c import main as index_c_main  # type: ignore


def _cc():
    return find_c_compiler()


def _patch_dump_counter(monkeypatch):
    """Count the capture module's real DB loads and AST dump invocations."""
    counter = {"n": 0, "loads": 0}
    real_dump = cap_mod.run_ast_dump_for_entry
    real_load = cap_mod.load_compile_entries

    def wrapped_dump(*args, **kwargs):
        counter["n"] += 1
        return real_dump(*args, **kwargs)

    def wrapped_load(*args, **kwargs):
        counter["loads"] += 1
        return real_load(*args, **kwargs)

    # Patch only the bound names that capture_clang_ast_package actually calls.
    # Patching both producer and source modules can make a counter pass even if
    # orchestration accidentally bypasses the shared capture.
    monkeypatch.setattr(cap_mod, "run_ast_dump_for_entry", wrapped_dump)
    monkeypatch.setattr(cap_mod, "load_compile_entries", wrapped_load)
    return counter


def _write_multi_entry_pkg(tmp_path: Path, n: int = 3) -> Path:
    """Synthetic package with N compile_commands entries (shared headers ok)."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    for i in range(n):
        (pkg / f"f{i}.c").write_text(
            f"static int h{i}(void) {{ return {i}; }}\n"
            f"int g{i}(void) {{ return h{i}(); }}\n",
            encoding="utf-8",
        )
    cc = _cc() or "clang"
    entries = []
    for i in range(n):
        entries.append(
            {
                "directory": str(pkg),
                "file": str(pkg / f"f{i}.c"),
                "arguments": [cc, "-c", f"f{i}.c", "-o", f"f{i}.o"],
            }
        )
    (pkg / "compile_commands.json").write_text(
        json.dumps(entries, indent=2) + "\n", encoding="utf-8"
    )
    return pkg


# ---------------------------------------------------------------------------
# Dump counts
# ---------------------------------------------------------------------------


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
@pytest.mark.parametrize(
    "sigs,calls,types,tuses",
    [flags for flags in product((False, True), repeat=4) if any(flags)],
)
def test_any_clang_flag_subset_one_dump_per_entry(
    tmp_path: Path,
    monkeypatch,
    sigs: bool,
    calls: bool,
    types: bool,
    tuses: bool,
):
    """Any non-empty subset of the four Clang flags dumps exactly N times."""
    n = 3
    pkg = _write_multi_entry_pkg(tmp_path, n=n)
    counter = _patch_dump_counter(monkeypatch)
    graph = tmp_path / f"g_{int(sigs)}{int(calls)}{int(types)}{int(tuses)}"
    index_c_main(
        package=pkg,
        graph=graph,
        keep_snapshots=2,
        compiler_builtins=False,
        compiler_dependencies=False,
        compiler_includes=False,
        clang_signatures=sigs,
        clang_calls=calls,
        clang_types=types,
        clang_type_uses=tuses,
        allow_toolchain_drift=False,
    )
    assert counter["n"] == n
    assert counter["loads"] == 1
    snap = (graph / "current").read_text(encoding="utf-8").strip()
    manifest = json.loads(
        (graph / "snapshots" / snap / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["clang_signatures"]["enabled"] is sigs
    assert manifest["clang_calls"]["enabled"] is calls
    assert manifest["clang_types"]["enabled"] is types
    assert manifest["clang_type_uses"]["enabled"] is tuses
    if sigs:
        assert manifest["clang_signatures"]["n_compile_entries"] == n
    if calls:
        assert manifest["clang_calls"]["n_compile_entries"] == n
    if types:
        assert manifest["clang_types"]["n_compile_entries"] == n
    if tuses:
        assert manifest["clang_type_uses"]["n_compile_entries"] == n
    assert not any(pkg.rglob("*.o"))
    assert not any(pkg.rglob("*.ast"))
    assert not any(pkg.rglob("*.d"))
    assert not any(pkg.rglob("*.i"))


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_neither_flag_zero_dumps(tmp_path: Path, monkeypatch):
    n = 2
    pkg = _write_multi_entry_pkg(tmp_path, n=n)
    counter = _patch_dump_counter(monkeypatch)
    graph = tmp_path / "g_off"
    index_c_main(
        package=pkg,
        graph=graph,
        keep_snapshots=2,
        compiler_builtins=False,
        compiler_dependencies=False,
        compiler_includes=False,
        clang_signatures=False,
        clang_calls=False,
        clang_types=False,
        clang_type_uses=False,
        allow_toolchain_drift=False,
    )
    assert counter["n"] == 0
    assert counter["loads"] == 0
    snap = (graph / "current").read_text(encoding="utf-8").strip()
    manifest = json.loads(
        (graph / "snapshots" / snap / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["clang_signatures"]["mode"] == "off"
    assert manifest["clang_calls"]["mode"] == "off"
    assert manifest["clang_types"]["mode"] == "off"
    assert manifest["clang_type_uses"]["mode"] == "off"


# ---------------------------------------------------------------------------
# Builder purity + report compatibility
# ---------------------------------------------------------------------------


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_builders_perform_zero_compiler_invocations(tmp_path: Path, monkeypatch):
    pkg = _write_multi_entry_pkg(tmp_path, n=2)
    counter = _patch_dump_counter(monkeypatch)
    capture = capture_clang_ast_package(pkg)
    n_after_capture = counter["n"]
    loads_after_capture = counter["loads"]
    assert n_after_capture == 2
    assert loads_after_capture == 1
    from c_clang_type_audit import (  # type: ignore
        build_type_declaration_audit_from_capture,
    )

    build_function_audit_from_capture(capture)
    build_call_audit_from_capture(capture)
    build_type_declaration_audit_from_capture(capture)
    assert counter["n"] == n_after_capture
    assert counter["loads"] == loads_after_capture


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_standalone_reports_equal_capture_built_reports():
    for name in ("inih", "cjson"):
        pkg = ROOT / "examples" / name
        standalone_fn = run_clang_ast_audit(pkg)
        standalone_call = run_clang_call_audit(pkg)
        capture = capture_clang_ast_package(pkg)
        from_cap_fn = build_function_audit_from_capture(capture)
        from_cap_call = build_call_audit_from_capture(capture)
        assert function_audit_to_json(standalone_fn) == function_audit_to_json(
            from_cap_fn
        )
        assert call_audit_to_json(standalone_call) == call_audit_to_json(
            from_cap_call
        )
        # Live count pins (host-stable for default compile DBs).
        if name == "inih":
            assert standalone_fn["counts"]["matched"] == 10
            assert standalone_call["counts"]["matched_internal"] == 16
        else:
            assert standalone_fn["counts"]["matched"] == 113
            assert standalone_call["counts"]["matched_internal"] == 188


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_capture_reuse_byte_identical_reports(tmp_path: Path):
    pkg = _write_multi_entry_pkg(tmp_path, n=2)
    capture = capture_clang_ast_package(pkg)
    a1 = function_audit_to_json(build_function_audit_from_capture(capture))
    a2 = function_audit_to_json(build_function_audit_from_capture(capture))
    b1 = call_audit_to_json(build_call_audit_from_capture(capture))
    b2 = call_audit_to_json(build_call_audit_from_capture(capture))
    assert a1 == a2
    assert b1 == b2
    assert_function_and_call_reports_agree(
        json.loads(a1), json.loads(b1), capture
    )


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_builder_order_independent_reports_and_ast_unchanged(tmp_path: Path):
    pkg = _write_multi_entry_pkg(tmp_path, n=2)
    capture = capture_clang_ast_package(pkg)
    ast_before = json.dumps(
        [entry.ast_root for entry in capture.entries],
        sort_keys=True,
        separators=(",", ":"),
    )
    r1 = build_function_audit_from_capture(capture)
    c1 = build_call_audit_from_capture(capture)
    c2 = build_call_audit_from_capture(capture)
    r2 = build_function_audit_from_capture(capture)
    ast_after = json.dumps(
        [entry.ast_root for entry in capture.entries],
        sort_keys=True,
        separators=(",", ":"),
    )
    assert function_audit_to_json(r1) == function_audit_to_json(r2)
    assert call_audit_to_json(c1) == call_audit_to_json(c2)
    assert ast_before == ast_after


# ---------------------------------------------------------------------------
# Combined parquet / failure behavior
# ---------------------------------------------------------------------------


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_combined_parquet_smoke_inih(tmp_path: Path):
    pkg = ROOT / "examples" / "inih"
    baseline = build_c_byog(pkg)
    graph = tmp_path / "g_inih_both"
    from c_clang_types import FACT_KIND as TYPE_FACT_KIND  # type: ignore

    index_c_main(
        package=pkg,
        graph=graph,
        keep_snapshots=2,
        compiler_builtins=False,
        compiler_dependencies=False,
        compiler_includes=False,
        clang_signatures=True,
        clang_calls=True,
        clang_types=True,
        clang_type_uses=False,
        allow_toolchain_drift=False,
    )
    snap = (graph / "current").read_text(encoding="utf-8").strip()
    snap_dir = graph / "snapshots" / snap
    import pandas as pd

    ents = pd.read_parquet(snap_dir / "entities.parquet")
    rels = pd.read_parquet(snap_dir / "relationships.parquet")
    assert len(ents) == len(baseline["entities"])
    assert len(rels) == len(baseline["relationships"])
    # Base ids/endpoints/types
    assert list(rels["id"]) == [r["id"] for r in baseline["relationships"]]
    assert list(rels["source"]) == [r["source"] for r in baseline["relationships"]]
    assert list(rels["target"]) == [r["target"] for r in baseline["relationships"]]
    assert list(rels["type"]) == [r["type"] for r in baseline["relationships"]]
    signed = ents[ents["clang_signature_status"].astype(str) == "matched"]
    assert len(signed) == 10
    assert (signed["clang_signature_fact_kind"] == SIG_FACT_KIND).all()
    calls = rels[rels["type"].astype(str) == "calls"]
    matched_calls = calls[calls["clang_call_status"].astype(str) == "matched"]
    assert len(matched_calls) == 16
    assert (matched_calls["clang_call_fact_kind"] == CALL_FACT_KIND).all()
    assert (matched_calls["confidence"] == 0.9).all()
    typed = ents[ents["clang_type_declaration_confirmed"] == True]  # noqa: E712
    assert len(typed) == 3
    assert (typed["clang_type_fact_kind"] == TYPE_FACT_KIND).all()
    manifest = json.loads((snap_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["clang_signatures"]["enabled"] is True
    assert manifest["clang_signatures"]["n_facts"] == 10
    assert manifest["clang_calls"]["enabled"] is True
    assert manifest["clang_calls"]["n_facts"] == 16
    assert manifest["clang_types"]["enabled"] is True
    assert manifest["clang_types"]["n_facts"] == 3
    assert "clang_ast_capture" not in manifest
    assert not any(pkg.rglob("*.o"))
    assert not any(pkg.rglob("*.ast"))


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_second_overlay_failure_publishes_no_snapshot(tmp_path: Path, monkeypatch):
    pkg = _write_multi_entry_pkg(tmp_path, n=1)
    graph = tmp_path / "g_fail"
    # Establish a valid prior publication. The failed combined run must leave
    # both its current pointer and its retained snapshot set unchanged.
    index_c_main(
        package=pkg,
        graph=graph,
        keep_snapshots=2,
        compiler_builtins=False,
        compiler_dependencies=False,
        compiler_includes=False,
        clang_signatures=False,
        clang_calls=False,
        clang_types=False,
        clang_type_uses=False,
        allow_toolchain_drift=False,
    )
    prior_current = (graph / "current").read_text(encoding="utf-8")
    prior_snapshots = sorted(p.name for p in (graph / "snapshots").iterdir())
    # Force call overlay to fail after signatures would have succeeded by
    # corrupting the built call report path: raise from append_clang_calls.
    import c_clang_calls as calls_mod

    def boom(*_a, **_k):
        raise calls_mod.ClangCallOverlayError("forced call overlay failure")

    monkeypatch.setattr(calls_mod, "append_clang_calls", boom)
    # Also patch index_c's bound name
    import index_c as index_mod

    monkeypatch.setattr(index_mod, "append_clang_calls", boom)

    import typer

    with pytest.raises((SystemExit, typer.Exit)) as ei:
        index_c_main(
            package=pkg,
            graph=graph,
            keep_snapshots=2,
            compiler_builtins=False,
            compiler_dependencies=False,
            compiler_includes=False,
            clang_signatures=True,
            clang_calls=True,
            clang_types=False,
            clang_type_uses=False,
            allow_toolchain_drift=False,
        )
    code = getattr(ei.value, "exit_code", None)
    if code is None:
        code = getattr(ei.value, "code", None)
    assert code == 2
    assert (graph / "current").read_text(encoding="utf-8") == prior_current
    assert sorted(
        p.name for p in (graph / "snapshots").iterdir()
    ) == prior_snapshots


def test_invalid_timeout_fails():
    for timeout in (0, -1, True, 1.5, "1"):
        with pytest.raises(
            ClangAstCaptureError, match="timeout must be a positive"
        ):
            capture_clang_ast_package(
                ROOT / "examples" / "inih", timeout=timeout  # type: ignore[arg-type]
            )


def test_package_mismatch_fails(tmp_path: Path):
    if _cc() is None:
        pytest.skip("no C compiler on PATH")
    pkg = _write_multi_entry_pkg(tmp_path, n=1)
    capture = capture_clang_ast_package(pkg)
    with pytest.raises(ClangAstCaptureError, match="does not match"):
        capture.assert_package(tmp_path / "other")


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_digest_compiler_mismatch_fails(tmp_path: Path):
    pkg = _write_multi_entry_pkg(tmp_path, n=1)
    capture = capture_clang_ast_package(pkg)
    fn = build_function_audit_from_capture(capture)
    call = build_call_audit_from_capture(capture)
    bad = dict(fn)
    bad["compile_commands_digest"] = "not-the-digest"
    with pytest.raises(ClangAstCaptureError, match="compile_commands_digest"):
        assert_function_and_call_reports_agree(bad, call, capture)
    bad2 = dict(call)
    bad2["compilers"] = [
        {
            "compiler_path": "/nonexistent/clang",
            "compiler_id": "fake",
            "compiler_version": "0",
        }
    ]
    with pytest.raises(ClangAstCaptureError, match="compiler provenance"):
        assert_function_and_call_reports_agree(fn, bad2, capture)
    bad3 = deepcopy(call)
    bad3["compilers"][0]["compiler_version"] = "different-version"
    with pytest.raises(ClangAstCaptureError, match="compiler provenance"):
        assert_function_and_call_reports_agree(fn, bad3, capture)
    bad4 = deepcopy(call)
    bad4["translation_units"][0]["file"] = "wrong.c"
    with pytest.raises(ClangAstCaptureError, match="translation-unit"):
        assert_function_and_call_reports_agree(fn, bad4, capture)


def test_missing_compile_db_fails(tmp_path: Path):
    pkg = tmp_path / "empty_pkg"
    pkg.mkdir()
    (pkg / "a.c").write_text("int f(void){return 0;}\n", encoding="utf-8")
    with pytest.raises(ClangAstCaptureError):
        capture_clang_ast_package(pkg)


def test_capture_repr_hides_ast(tmp_path: Path):
    if _cc() is None:
        pytest.skip("no C compiler on PATH")
    pkg = _write_multi_entry_pkg(tmp_path, n=1)
    capture = capture_clang_ast_package(pkg)
    text = repr(capture) + str(capture)
    for ent in capture.entries:
        text += repr(ent) + str(ent)
    assert "TranslationUnitDecl" not in text
    assert "FunctionDecl" not in text
    assert "ast_root" not in text
    # AST payload must not leak via default dataclass field rendering.
    assert "kind" not in text


@pytest.mark.skipif(_cc() is None, reason="no C compiler on PATH")
def test_capture_does_not_leave_artifacts(tmp_path: Path):
    pkg = _write_multi_entry_pkg(tmp_path, n=2)
    before = {p.relative_to(pkg).as_posix() for p in pkg.rglob("*") if p.is_file()}
    capture = capture_clang_ast_package(pkg)
    build_function_audit_from_capture(capture)
    build_call_audit_from_capture(capture)
    after = {p.relative_to(pkg).as_posix() for p in pkg.rglob("*") if p.is_file()}
    assert before == after
    assert not any(pkg.rglob("*.o"))
    assert not any(pkg.rglob("*.ast"))
    assert not any(pkg.rglob("*.d"))
    assert not any(pkg.rglob("*.i"))
