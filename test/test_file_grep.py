"""Tests for /api/file-grep — the side-panel Files rail's content search.

Three properties carry the endpoint, and each has its own class below:

1. **Gating.** The root goes through the same ``_validate_dashboard_path`` /
   ``is_sensitive_path`` chokepoint every other handler in ``files.py`` uses, and
   a hit is filtered by ``is_sensitive_path`` again on the way out — so a
   credential store is unreachable whichever engine ran.
2. **Engine parity.** ripgrep and the python fallback must answer the SAME tree
   the same way. They are separate implementations of one contract, and a host
   without ``rg`` must not get a different search.
3. **Budgets.** A result cap and a wall-clock deadline both report ``truncated``,
   and the document pass reports ``skipped_docs`` rather than silently omitting
   documents it never reached.

The document fixtures are built by hand (a ZIP holding the XML parts a parser
reads, a minimal uncompressed PDF) so the suite needs no authoring library and no
binary fixture committed to the repo.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers import api_file_grep
from kiro_crew.dashboard.handlers import files as f

#: Skips the ripgrep half of a parity pair on a host without the binary. The
#: python half always runs, so the fallback is never the untested engine.
requires_rg = pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")


def _make_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/api/file-grep", api_file_grep)
    state = MagicMock()
    state.file_indexes.get.return_value = None
    app["state"] = state
    return app


@pytest.fixture(autouse=True)
def mock_sel():
    with patch("kiro_crew.dashboard.handlers.sel") as m:
        m.return_value = MagicMock()
        yield m.return_value


async def _get(root: Path | str, query: str, **params: str) -> tuple[int, dict]:
    """One request against a fresh app. The client is built INSIDE the coroutine
    because ``TestClient`` binds to the running loop at construction."""
    parts = [f"root={root}", f"q={query}", *[f"{k}={v}" for k, v in params.items()]]
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.get("/api/file-grep?" + "&".join(parts))
        return resp.status, await resp.json()


async def _grep(root: Path | str, query: str, **params: str) -> dict:
    status, payload = await _get(root, query, **params)
    assert status == 200, payload
    return payload


def _files(payload: dict) -> set[str]:
    return {os.path.basename(r["file"]) for r in payload["results"]}


@pytest.fixture()
def tree(tmp_path: Path) -> Path:
    """A small text tree: three matches, one non-match, two ignored dirs, one
    binary."""
    root = tmp_path / "proj"
    root.mkdir()
    (root / "alpha.py").write_text("import os\nNEEDLE lives here\nafter\n", encoding="utf-8")
    (root / "notes.md").write_text("# heading\n\nprose about needle in md\n", encoding="utf-8")
    (root / "other.txt").write_text("nothing to find\n", encoding="utf-8")
    sub = root / "pkg"
    sub.mkdir()
    (sub / "deep.py").write_text("a\nb\nc\nd needle at four\n", encoding="utf-8")
    for ignored in ("node_modules", ".git"):
        d = root / ignored
        d.mkdir()
        (d / "vendored.py").write_text("needle should not be reported\n", encoding="utf-8")
    (root / "blob.bin").write_bytes(b"needle\x00\x00binary\n")
    # A .gitignore and a file it covers. ripgrep honours ignore files by default
    # and the fallback's os.walk cannot, so without this the parity tests agreed
    # only because no fixture ever carried one.
    (root / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
    (root / "ignored.py").write_text("needle in an ignored file\n", encoding="utf-8")
    return root


# ── document fixtures ────────────────────────────────────────────────────────


def _write_docx(path: Path, paragraphs: list[str]) -> None:
    body = "".join(f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>" for text in paragraphs)
    document = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>"
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("word/document.xml", document)


def _write_pptx(path: Path, slides: list[str]) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        for number, text in enumerate(slides, 1):
            slide = (
                '<?xml version="1.0" encoding="UTF-8"?>'
                "<p:sld"
                ' xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"'
                ' xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
                f"<a:t>{text}</a:t></p:sld>"
            )
            zf.writestr(f"ppt/slides/slide{number}.xml", slide)


def _write_pdf(path: Path, pages: list[str]) -> None:
    """A minimal uncompressed PDF whose text sits in ``(...)`` literals.

    Enough for pdfplumber to parse it as a real document.
    """
    page_ids = [4 + 2 * i for i in range(len(pages))]
    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    objects: list[bytes] = [
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n",
        f"2 0 obj\n<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>\nendobj\n".encode(),
        b"3 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n",
    ]
    for pid, text in zip(page_ids, pages):
        stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
        objects.append(
            (
                f"{pid} 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                f"/Resources << /Font << /F1 3 0 R >> >> /Contents {pid + 1} 0 R >>\nendobj\n"
            ).encode()
        )
        objects.append(
            f"{pid + 1} 0 obj\n<< /Length {len(stream)} >>\nstream\n".encode()
            + stream
            + b"\nendstream\nendobj\n"
        )
    out = bytearray(b"%PDF-1.4\n")
    for obj in objects:
        out += obj
    out += b"trailer\n<< /Root 1 0 R >>\n%%EOF\n"
    path.write_bytes(bytes(out))


@pytest.fixture()
def docs(tmp_path: Path) -> Path:
    root = tmp_path / "docs"
    root.mkdir()
    _write_docx(root / "spec.docx", ["intro", "the WIDGET decision", "outro"])
    _write_pptx(root / "deck.pptx", ["cover", "agenda", "the WIDGET roadmap"])
    _write_pdf(root / "paper.pdf", ["first page", "second page WIDGET here"])
    return root


# ── gating ───────────────────────────────────────────────────────────────────


class TestGating:
    @pytest.mark.asyncio
    async def test_a_short_query_is_not_a_search(self, tree):
        """One character matches nearly every file, which is a whole-tree read
        rather than a result set — the same floor /api/file-search applies."""
        payload = await _grep(tree, "n")
        assert payload["results"] == []
        assert payload["root"] == ""
        # No engine ran, so none is named. Answering "rg" here would have cost a
        # $PATH walk on the event loop once per keystroke to say something the
        # caller cannot use.
        assert payload["engine"] == ""

    @pytest.mark.asyncio
    async def test_a_missing_root_is_not_a_search(self):
        assert (await _grep("", "needle"))["results"] == []

    @pytest.mark.asyncio
    async def test_an_overlong_query_is_refused(self, tree):
        payload = await _grep(tree, "x" * (f._GREP_MAX_QUERY_CHARS + 1))
        assert payload["results"] == []

    @pytest.mark.asyncio
    async def test_a_sensitive_root_is_denied_not_searched(self, tree, mock_sel):
        """403, audited as denied. Not 404: telling the caller a credential store
        is 'not found' invites it to probe for the spelling that is allowed."""
        with patch.object(f, "is_sensitive_path", lambda p: True):
            status, payload = await _get(tree, "needle")
        assert status == 403
        assert payload["error"] == "Access denied"
        # The dashboard renders `error` into a localized UI; `code` is the
        # contract the error-code ratchet guards.
        assert payload["code"] == "sensitive_path"
        assert mock_sel.log_api_access.call_args.kwargs["outcome"] == "denied"

    @pytest.mark.asyncio
    async def test_a_root_the_validator_refuses_is_denied(self, tree):
        """A refused name never becomes a search root. The validator is the gate
        this whole handler family shares, so its refusal must land as 403."""
        with patch.object(f, "_validate_dashboard_path", lambda raw: None):
            status, _ = await _get(tree, "needle")
        assert status == 403

    @pytest.mark.asyncio
    async def test_a_file_root_is_a_404_not_a_search(self, tree):
        status, payload = await _get(tree / "alpha.py", "needle")
        assert status == 404
        assert payload["results"] == []
        assert payload["code"] == "not_a_directory"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("engine", ["python", pytest.param("rg", marks=requires_rg)])
    async def test_a_sensitive_hit_is_filtered_out_by_either_engine(
        self, tree, engine, monkeypatch
    ):
        """The per-hit filter is the authority. ripgrep's exclusion globs only
        keep it from reading those bytes; a hit that reaches the parser anyway
        must still be dropped, and the fallback has no globs at all."""
        if engine == "python":
            monkeypatch.setattr(f, "_grep_rg_executable", lambda: None)
        secret = str(tree / "alpha.py")
        real = f.is_sensitive_path
        monkeypatch.setattr(
            f, "is_sensitive_path", lambda p: os.path.realpath(p) == secret or real(p)
        )
        payload = await _grep(tree, "needle")
        assert "alpha.py" not in _files(payload)
        assert "deep.py" in _files(payload)


# ── engine parity ────────────────────────────────────────────────────────────


class TestEngines:
    @pytest.mark.asyncio
    async def test_the_python_fallback_reports_file_line_and_preview(self, tree, monkeypatch):
        monkeypatch.setattr(f, "_grep_rg_executable", lambda: None)
        payload = await _grep(tree, "needle")
        assert payload["engine"] == "python"
        assert payload["root"] == str(tree)
        hit = next(r for r in payload["results"] if r["file"].endswith("alpha.py"))
        assert hit["line"] == 2
        assert hit["preview"] == "NEEDLE lives here"
        assert "label" not in hit  # a text hit has a line to jump to
        # No column: the rail finds the match inside the preview itself, and a
        # second answer to the same question is a second thing to get wrong.
        assert "col" not in hit

    @pytest.mark.asyncio
    @requires_rg
    async def test_ripgrep_reports_the_same_shape(self, tree):
        payload = await _grep(tree, "needle")
        assert payload["engine"] == "rg"
        hit = next(r for r in payload["results"] if r["file"].endswith("alpha.py"))
        assert hit["line"] == 2
        assert hit["preview"] == "NEEDLE lives here"
        assert "col" not in hit

    @pytest.mark.asyncio
    @requires_rg
    async def test_both_engines_answer_one_tree_identically(self, tree, monkeypatch):
        """The parity that matters: same files, same lines. A host without
        ripgrep must not get a different search, so the two engines are compared
        against EACH OTHER on one tree rather than each against its own
        expectations."""
        with_rg = await _grep(tree, "needle")
        monkeypatch.setattr(f, "_grep_rg_executable", lambda: None)
        without_rg = await _grep(tree, "needle")
        assert with_rg["engine"] == "rg"
        assert without_rg["engine"] == "python"

        def key(payload: dict) -> list[tuple[str, int]]:
            return sorted((os.path.basename(r["file"]), r["line"]) for r in payload["results"])

        assert key(with_rg) == key(without_rg)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("engine", ["python", pytest.param("rg", marks=requires_rg)])
    async def test_ignored_directories_and_binaries_are_never_reported(
        self, tree, engine, monkeypatch
    ):
        if engine == "python":
            monkeypatch.setattr(f, "_grep_rg_executable", lambda: None)
        assert _files(await _grep(tree, "needle")) == {
            "alpha.py",
            "notes.md",
            "deep.py",
            "ignored.py",
        }

    @pytest.mark.asyncio
    @pytest.mark.parametrize("engine", ["python", pytest.param("rg", marks=requires_rg)])
    async def test_a_multibyte_line_survives_both_engines_intact(
        self, tmp_path, engine, monkeypatch
    ):
        """``rg --json`` is UTF-8 by definition, so the preview must come back as
        the file's own characters rather than mojibake from a host-locale decode."""
        if engine == "python":
            monkeypatch.setattr(f, "_grep_rg_executable", lambda: None)
        root = tmp_path / "utf8"
        root.mkdir()
        (root / "wide.txt").write_text("αβγδ needle ε\n", encoding="utf-8")
        payload = await _grep(root, "needle")
        assert payload["results"][0]["preview"] == "αβγδ needle ε"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("engine", ["python", pytest.param("rg", marks=requires_rg)])
    async def test_the_query_is_a_literal_not_a_regex_on_either_engine(
        self, tmp_path, engine, monkeypatch
    ):
        """The fallback matches ``re.escape(query)`` and the document pass uses
        ``str.find``, so ripgrep must match literally too. Unfixed, ``a.c`` was a
        wildcard on an rg host and three characters on every other host."""
        if engine == "python":
            monkeypatch.setattr(f, "_grep_rg_executable", lambda: None)
        root = tmp_path / "regex"
        root.mkdir()
        (root / "literal.txt").write_text("a.c is here\n", encoding="utf-8")
        (root / "wildcard.txt").write_text("abc is not the same string\n", encoding="utf-8")
        assert _files(await _grep(root, "a.c")) == {"literal.txt"}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("engine", ["python", pytest.param("rg", marks=requires_rg)])
    async def test_an_unbalanced_query_is_a_search_not_a_parse_error(
        self, tmp_path, engine, monkeypatch
    ):
        """``config(`` is an ordinary thing to look for in code. As a regex it does
        not compile, and ripgrep's error exit under ``--no-messages`` is an empty
        stdout — which read as an authoritative "no matches"."""
        if engine == "python":
            monkeypatch.setattr(f, "_grep_rg_executable", lambda: None)
        root = tmp_path / "unbalanced"
        root.mkdir()
        (root / "call.py").write_text("value = config(key)\n", encoding="utf-8")
        assert _files(await _grep(root, "config(")) == {"call.py"}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("engine", ["python", pytest.param("rg", marks=requires_rg)])
    async def test_case_is_ignored_whatever_the_query_looks_like(
        self, tmp_path, engine, monkeypatch
    ):
        """Both engines fold case UNCONDITIONALLY. Under ripgrep's --smart-case a
        capital in the query silently made text files case-sensitive while the
        document pass kept folding, so one response disagreed with itself."""
        if engine == "python":
            monkeypatch.setattr(f, "_grep_rg_executable", lambda: None)
        root = tmp_path / "casing"
        root.mkdir()
        (root / "lower.txt").write_text("the widget ships\n", encoding="utf-8")
        (root / "upper.txt").write_text("the WIDGET ships\n", encoding="utf-8")
        assert _files(await _grep(root, "Widget")) == {"lower.txt", "upper.txt"}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("engine", ["python", pytest.param("rg", marks=requires_rg)])
    async def test_a_file_over_the_size_ceiling_is_skipped_by_both_engines(
        self, tmp_path, engine, monkeypatch
    ):
        """The fallback reads a bounded prefix, so without ``--max-filesize``
        ripgrep scanned a big log to EOF and reported a match the other host never
        saw. Both now skip the file: a bounded engine that silently searched HALF
        a file would be the worse divergence."""
        if engine == "python":
            monkeypatch.setattr(f, "_grep_rg_executable", lambda: None)
        monkeypatch.setattr(f, "_GREP_MAX_FILE_BYTES", 64)
        root = tmp_path / "big"
        root.mkdir()
        (root / "small.txt").write_text("needle\n", encoding="utf-8")
        (root / "large.log").write_text("x" * 200 + "\nneedle at the end\n", encoding="utf-8")
        assert _files(await _grep(root, "needle")) == {"small.txt"}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("engine", ["python", pytest.param("rg", marks=requires_rg)])
    async def test_an_ignore_file_does_not_change_the_answer(self, tree, engine, monkeypatch):
        """ripgrep honours .gitignore by default and the fallback's os.walk cannot,
        so the same project answered differently depending on which host ran it.
        The noisy directories are pruned by _WALK_SKIP_DIRS on both sides instead —
        the half of the ignore semantics the two engines can agree on."""
        if engine == "python":
            monkeypatch.setattr(f, "_grep_rg_executable", lambda: None)
        assert "ignored.py" in _files(await _grep(tree, "needle"))


# ── budgets ──────────────────────────────────────────────────────────────────


class TestBudgets:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("engine", ["python", pytest.param("rg", marks=requires_rg)])
    async def test_the_result_cap_reports_truncated(self, tmp_path, engine, monkeypatch):
        if engine == "python":
            monkeypatch.setattr(f, "_grep_rg_executable", lambda: None)
        root = tmp_path / "many"
        root.mkdir()
        for i in range(6):
            (root / f"f{i}.txt").write_text("needle\n", encoding="utf-8")
        monkeypatch.setattr(f, "_GREP_MAX_RESULTS", 3)
        payload = await _grep(root, "needle")
        assert len(payload["results"]) == 3
        assert payload["truncated"] is True

    @pytest.mark.asyncio
    async def test_a_spent_deadline_reports_truncated_rather_than_no_matches(
        self, tree, monkeypatch
    ):
        """An exhausted budget is not an empty result set. Reporting one as the
        other would tell the user the text is absent when the search stopped."""
        monkeypatch.setattr(f, "_grep_rg_executable", lambda: None)
        monkeypatch.setattr(f, "_GREP_TIME_BUDGET_SECS", -1.0)
        assert (await _grep(tree, "needle"))["truncated"] is True

    @pytest.mark.asyncio
    async def test_a_spent_deadline_counts_documents_it_never_opened(self, docs, monkeypatch):
        """``skipped_docs`` is what keeps 'no document matched' distinguishable
        from 'the budget ran out before the documents'. It is a FLOOR: the walk
        ENDS on a spent deadline rather than counting its way through the tree,
        and ``truncated`` is what says the number is not a total."""
        monkeypatch.setattr(f, "_grep_rg_executable", lambda: None)
        monkeypatch.setattr(f, "_GREP_TIME_BUDGET_SECS", -1.0)
        payload = await _grep(docs, "widget")
        assert payload["skipped_docs"] >= 1
        assert payload["truncated"] is True
        assert payload["results"] == []

    @pytest.mark.asyncio
    async def test_a_spent_deadline_stops_the_document_walk_rather_than_counting_on(
        self, tmp_path, monkeypatch
    ):
        """Continuing past the deadline holds a bounded transfer worker for up to
        ``_GREP_MAX_DIRS_VISITED`` directories after the budget is already gone,
        and at keystroke rate that starves the pool every other file endpoint
        shares."""
        root = tmp_path / "deep"
        root.mkdir()
        here = root
        for level in range(6):
            here = here / f"level{level}"
            here.mkdir()
        visited: list[str] = []
        real_walk = f.os.walk

        def counting_walk(top, *args, **kwargs):
            for entry in real_walk(top, *args, **kwargs):
                visited.append(entry[0])
                yield entry

        monkeypatch.setattr(f, "_grep_rg_executable", lambda: None)
        monkeypatch.setattr(f, "_GREP_TIME_BUDGET_SECS", -1.0)
        monkeypatch.setattr(f.os, "walk", counting_walk)
        await _grep(root, "widget")
        # Both passes may receive the root from os.walk, then must stop before
        # advancing into the six document-free child directories.
        assert len(visited) <= 2, visited

    @pytest.mark.asyncio
    async def test_an_oversized_document_is_skipped_not_parsed(self, docs, monkeypatch):
        monkeypatch.setattr(f, "_GREP_DOC_MAX_BYTES", 1)
        assert (await _grep(docs, "widget"))["skipped_docs"] == 3

    @pytest.mark.asyncio
    async def test_a_busy_probe_pool_is_a_coded_503(self, tree, monkeypatch):
        async def _busy(*args, **kwargs):
            raise f._PathProbeBusy()

        monkeypatch.setattr(f, "_run_path_probe", _busy)
        status, payload = await _get(tree, "needle")
        assert status == 503
        assert payload["code"] == "path_probe_busy"


# ── the document pass ────────────────────────────────────────────────────────


class TestDocumentPass:
    @pytest.mark.asyncio
    async def test_a_word_document_reports_a_doc_label_and_no_line(self, docs):
        payload = await _grep(docs, "widget")
        hit = next(r for r in payload["results"] if r["file"].endswith("spec.docx"))
        # A .docx paragraph carries no page or section a reader can navigate to,
        # so the row says so rather than inventing a line number.
        assert hit["label"] == "doc"
        assert hit["line"] == 0
        assert "WIDGET" in hit["preview"]

    @pytest.mark.asyncio
    async def test_a_deck_reports_the_slide_it_matched(self, docs):
        payload = await _grep(docs, "widget")
        hit = next(r for r in payload["results"] if r["file"].endswith("deck.pptx"))
        assert hit["label"] == "slide 3"
        assert "WIDGET" in hit["preview"]

    @pytest.mark.asyncio
    async def test_a_pdf_reports_the_page_it_matched(self, docs):
        pytest.importorskip("pdfplumber")
        payload = await _grep(docs, "widget")
        hit = next(r for r in payload["results"] if r["file"].endswith("paper.pdf"))
        assert hit["label"] == "p 2"
        assert hit["line"] == 0

    @pytest.mark.asyncio
    async def test_one_unextractable_page_costs_only_itself(self, tmp_path, monkeypatch):
        """A page that raises costs its own page and nothing else.

        The blast radius is why this is pinned rather than left to the outer
        handler: that handler returns (), which loses the pages already read AND
        every later page, and the empty result is then cached under the content
        digest -- so a single bad page would empty the file for the life of the
        process, not merely for the request that met it."""
        pytest.importorskip("pdfplumber")
        import pdfplumber

        root = tmp_path / "pdf-partial"
        root.mkdir()
        path = root / "paper.pdf"
        _write_pdf(path, ["page one WIDGET", "page two", "page three GADGET"])
        f._GREP_DOC_CACHE.clear()

        real_open = pdfplumber.open

        def flaky_open(stream, *a, **kw):
            pdf = real_open(stream, *a, **kw)
            pages = list(pdf.pages)
            if len(pages) >= 2:

                def boom():
                    raise RuntimeError("this page is not extractable")

                pages[1].extract_text = boom  # type: ignore[method-assign]
            return pdf

        monkeypatch.setattr(pdfplumber, "open", flaky_open)

        # The page BEFORE the bad one and the page AFTER it are both still found.
        assert _files(await _grep(root, "widget")) == {"paper.pdf"}
        assert _files(await _grep(root, "gadget")) == {"paper.pdf"}

    @pytest.mark.asyncio
    async def test_a_workbook_reports_its_sheet_and_row(self, tmp_path):
        openpyxl = pytest.importorskip("openpyxl")
        root = tmp_path / "sheets"
        root.mkdir()
        book = openpyxl.Workbook()
        sheet = book.active
        sheet.title = "Ledger"
        sheet.append(["date", "note"])
        sheet.append(["2026-01-01", "the WIDGET invoice"])
        book.save(root / "book.xlsx")
        payload = await _grep(root, "widget")
        hit = next(r for r in payload["results"] if r["file"].endswith("book.xlsx"))
        assert hit["label"] == "Ledger row 2"
        assert "WIDGET" in hit["preview"]

    def test_a_workbook_with_a_bloated_inventory_is_refused_before_openpyxl(
        self, tmp_path, monkeypatch
    ):
        """openpyxl opens the container itself, so without the inventory vet a
        crafted workbook reaches the XML parser on its extension alone."""
        pytest.importorskip("openpyxl")
        path = tmp_path / "bomb.xlsx"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("xl/workbook.xml", "<workbook/>")
        monkeypatch.setattr(f, "_SHEET_MAX_MEMBERS", 0)
        assert f._grep_xlsx_segments(path.read_bytes(), str(path)) == ()

    @pytest.mark.asyncio
    async def test_the_text_pass_never_reports_a_document_twice(self, docs):
        """Both engines skip the document extensions, so a container is named by
        exactly one pass — the one that can say WHERE inside it the match is."""
        payload = await _grep(docs, "widget")
        assert len(payload["results"]) == len({r["file"] for r in payload["results"]})
        assert all(r.get("label") for r in payload["results"])

    @pytest.mark.asyncio
    async def test_extraction_is_cached_by_the_content_it_parsed(self, docs, monkeypatch):
        """The rail re-queries on every keystroke; re-parsing a deck per
        character is the entire cost this pass would otherwise add."""
        f._GREP_DOC_CACHE.clear()
        calls: list[str] = []
        real = f._grep_doc_segments

        def counting(data: bytes, path: str, ext: str):
            calls.append(path)
            return real(data, path, ext)

        monkeypatch.setattr(f, "_grep_doc_segments", counting)
        await _grep(docs, "widget")
        first = len(calls)
        assert first == 3
        await _grep(docs, "roadmap")
        assert len(calls) == first, "second query re-parsed cached documents"

        # An edit must invalidate: different bytes hash to a different key.
        _write_docx(docs / "spec.docx", ["intro", "a different WIDGET line", "x" * 64])
        await _grep(docs, "widget")
        assert len(calls) > first

    @pytest.mark.asyncio
    async def test_a_same_size_edit_within_one_second_invalidates_the_cache(self, tmp_path):
        root = tmp_path / "same-second"
        root.mkdir()
        path = root / "spec.docx"
        _write_docx(path, ["the WIDGET decision"])
        stamp = path.stat().st_mtime_ns
        second = stamp - (stamp % 1_000_000_000)
        os.utime(path, ns=(stamp, second + 100))
        f._GREP_DOC_CACHE.clear()
        assert _files(await _grep(root, "widget")) == {"spec.docx"}

        size = path.stat().st_size
        _write_docx(path, ["the GADGET decision"])
        assert path.stat().st_size == size
        os.utime(path, ns=(stamp, second + 200))
        assert _files(await _grep(root, "gadget")) == {"spec.docx"}

    @pytest.mark.asyncio
    async def test_a_replacement_that_forges_mtime_and_size_cannot_serve_the_old_parse(
        self, tmp_path
    ):
        """An atomic replace can land between the read and any separate stat, so a
        key derived from stat metadata has a window in which the OLD bytes' segments
        are stored under the NEW file's identity, and every later search answers
        from the stale parse. Here that window is made total -- mtime and size are
        forged identical -- and the search must still read the new content, because
        the key is a digest of the bytes the segments came from."""
        root = tmp_path / "forged"
        root.mkdir()
        path = root / "spec.docx"
        _write_docx(path, ["the WIDGET decision"])
        before = path.stat()
        f._GREP_DOC_CACHE.clear()
        assert _files(await _grep(root, "widget")) == {"spec.docx"}

        _write_docx(path, ["the GADGET decision"])
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        after = path.stat()
        assert after.st_size == before.st_size
        assert after.st_mtime_ns == before.st_mtime_ns
        assert _files(await _grep(root, "gadget")) == {"spec.docx"}
        assert _files(await _grep(root, "widget")) == set()

    @pytest.mark.asyncio
    async def test_document_matching_uses_ignorecase_without_unicode_expansion(self, tmp_path):
        root = tmp_path / "unicode"
        root.mkdir()
        _write_docx(root / "street.docx", ["Straße"])
        assert _files(await _grep(root, "strasse")) == set()

    def test_the_cache_evicts_least_recently_used(self):
        f._GREP_DOC_CACHE.clear()
        for i in range(f._GREP_DOC_CACHE_ENTRIES + 5):
            f._grep_doc_cache_put((".docx", str(i).encode()), (("doc", "x"),))
        assert len(f._GREP_DOC_CACHE) == f._GREP_DOC_CACHE_ENTRIES
        assert f._grep_doc_cache_get((".docx", b"0")) is None
        newest = (".docx", str(f._GREP_DOC_CACHE_ENTRIES + 4).encode())
        assert f._grep_doc_cache_get(newest) is not None


def _rg_spawn(
    monkeypatch, *, lines=(), returncode=0, wait_raises=False, patch_wrap=True, made=None
):
    """Point `_grep_rg` at a fake streaming child.

    `_grep_rg` reads `proc.stdout` line by line, so the fake's stdout is any
    iterable of record lines -- including an endless generator, which is how the
    cap is tested without materializing anything.
    """
    if patch_wrap:
        monkeypatch.setattr(f, "_grep_rg_executable", lambda: "/usr/bin/rg")
        monkeypatch.setattr(f, "wrap_argv", lambda cmd: (cmd, None))
        monkeypatch.setattr(f, "cgroup_scope_argv", lambda cmd: cmd)

    class _Pipe:
        """Iterable like a text pipe, and closable like one."""

        def __init__(self, source):
            self._it = iter(source)
            self.closed = False

        def __iter__(self):
            return self

        def __next__(self):
            if self.closed:
                raise StopIteration
            return next(self._it)

        def close(self):
            self.closed = True

    class _Proc:
        pid = 4242

        def __init__(self):
            self.stdout = _Pipe(lines)
            self._reaped = False

        def poll(self):
            # None means "still running", which is what makes teardown kill it.
            return None if not self._reaped else returncode

        def wait(self, timeout=None):
            if wait_raises and not self._reaped:
                self._reaped = True
                raise subprocess.TimeoutExpired(cmd="rg", timeout=timeout or 1.0)
            self._reaped = True
            return returncode

    def _spawn(argv, **kw):
        proc = _Proc()
        if made is not None:
            made.append(proc)
        return proc

    monkeypatch.setattr(f, "popen_limited", _spawn)
    # Every path that stops the child early goes through this, so the helper owns
    # it rather than each test remembering: the fake pid is not a real process.
    killed: list[int] = []
    monkeypatch.setattr(f.platform_compat, "kill_process_tree", lambda pid: killed.append(pid))
    return killed


def _windows_rg(monkeypatch, path, *, sid="S-1-5-21-me", acl_error=None, roots=()):
    """Point `_grep_rg_executable` at *path* as though this were Windows.

    The ACL verdict is injected rather than read: `windows_acl.describe` needs a
    real security descriptor, and the decision under test is what this resolver
    does with the answer, not how the descriptor is parsed.
    """
    monkeypatch.setattr(f.sys, "platform", "win32")
    monkeypatch.setattr(f.shutil, "which", lambda name: str(path))
    monkeypatch.setattr(f.platform_compat, "current_user_sid", lambda: sid or None)
    # Patched on `f`, not on github_runner: the imports are module-scope now, so
    # files.py holds its own binding and patching the source module misses it.
    monkeypatch.setattr(f, "agent_writable_roots", lambda: roots)

    def _check(target, *, label, me_sid, strict):
        if acl_error:
            raise ValueError(acl_error)

    monkeypatch.setattr(f, "check_provider_path_component_windows", _check)


# ── helpers ──────────────────────────────────────────────────────────────────


class TestPreviewRedaction:
    """A preview is file content on its way to a screen.

    Unlike a read, the user never asked for this particular file: they asked
    "which file says X", and the answer quotes the line back. A project source
    file with an API key pasted into it therefore puts that key in the response,
    and the sensitive-path fence does not cover it -- that guards credential
    STORES, not a secret in ordinary code.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("engine", ["python", pytest.param("rg", marks=requires_rg)])
    async def test_a_credential_on_the_matching_line_is_redacted(
        self, tmp_path, engine, monkeypatch
    ):
        if engine == "python":
            monkeypatch.setattr(f, "_grep_rg_executable", lambda: None)
        root = tmp_path / "secrets"
        root.mkdir()
        (root / "settings.py").write_text(
            'AWS_KEY = "AKIAIOSFODNN7EXAMPLE"  # deploy key\n', encoding="utf-8"
        )
        payload = await _grep(root, "deploy key")
        preview = payload["results"][0]["preview"]
        assert "AKIAIOSFODNN7EXAMPLE" not in preview
        assert "deploy key" in preview  # the match itself still reads

    @pytest.mark.asyncio
    async def test_a_credential_inside_a_document_is_redacted_too(self, tmp_path):
        """The document pass and the text pass share ``_grep_hit``, which is why
        the redaction lives there rather than at each engine: one chokepoint, so
        a third pass cannot be added without it."""
        root = tmp_path / "docsecret"
        root.mkdir()
        _write_docx(
            root / "runbook.docx",
            ["the WIDGET rotation", "token AKIAIOSFODNN7EXAMPLE is the WIDGET key"],
        )
        payload = await _grep(root, "WIDGET key")
        preview = payload["results"][0]["preview"]
        assert "AKIAIOSFODNN7EXAMPLE" not in preview

    def test_a_credential_in_a_sheet_title_is_redacted_like_a_preview(self):
        """A label is document CONTENT, not a shape this endpoint chose -- a workbook's
        is its sheet title, which an author picks. It reaches the response and the audit
        resource string, so leaving it raw published whatever the title said."""
        hit = f._grep_hit("/r/budget.xlsx", 0, "totals", label="AKIAIOSFODNN7EXAMPLE row 12")
        assert "AKIA" not in hit["label"]

    def test_a_long_sheet_title_is_cut_like_a_preview(self):
        """Author-controlled text of any length, bounded like every other string this
        row carries."""
        hit = f._grep_hit("/r/b.xlsx", 0, "totals", label="s" * 4000 + " row 1")
        assert len(hit["label"]) <= f._GREP_LABEL_CHARS

    def test_an_oversize_record_is_skipped_and_the_answer_says_it_is_short(self, monkeypatch):
        """The queue bounds how MANY records it holds, not how big they are.
        `--max-count 1` bounds records per file and `--max-filesize` bounds a file at
        2 MB, so one matching line inside a minified blob is a multi-megabyte record and
        a full queue of them is hundreds of MB. Such a record is skipped -- its 400-char
        preview carries nothing actionable -- and the answer is marked short."""
        big = (
            json.dumps(
                {
                    "type": "match",
                    "data": {
                        "path": {"text": "/r/bundle.min.js"},
                        "line_number": 1,
                        "lines": {"text": "needle" + "x" * (f._GREP_RG_MAX_RECORD_BYTES + 10)},
                    },
                }
            )
            + "\n"
        )
        small = (
            json.dumps(
                {
                    "type": "match",
                    "data": {
                        "path": {"text": "/r/a.py"},
                        "line_number": 7,
                        "lines": {"text": "needle here"},
                    },
                }
            )
            + "\n"
        )

        _rg_spawn(monkeypatch, lines=(big, small))
        hits, truncated = f._grep_rg("/r", "needle", f.time.monotonic() + 5)
        # The ordinary record still lands ...
        assert [h["file"] for h in hits] == ["/r/a.py"]
        # ... and the oversize one is reported as incompleteness, not dropped silently.
        assert truncated is True

    def test_redaction_runs_before_truncation(self, monkeypatch):
        """A secret straddling the preview cap would be CUT IN HALF first, and
        half a token matches no pattern -- so the cut has to come second."""
        monkeypatch.setattr(f, "_GREP_PREVIEW_CHARS", 40)
        line = "x" * 30 + "AKIAIOSFODNN7EXAMPLE" + " tail"
        hit = f._grep_hit("/r/a.py", 1, line)
        assert "AKIA" not in hit["preview"]
        assert len(hit["preview"]) <= 40


class TestTrustedRipgrep:
    """The gateway's $PATH can reach trees the agent writes. An ``rg`` planted
    there would run on the user's next keystroke with the gateway's own
    environment, so the resolved binary is vetted before it is ever spawned."""

    def test_an_rg_inside_an_agent_writable_tree_is_refused(self, tmp_path, monkeypatch):
        planted = tmp_path / "proj" / "node_modules" / ".bin"
        planted.mkdir(parents=True)
        fake = planted / "rg"
        fake.write_text("#!/bin/sh\nexit 0\n")
        fake.chmod(0o755)
        monkeypatch.setattr(f.shutil, "which", lambda name: str(fake))
        monkeypatch.setattr(f, "agent_writable_roots", lambda: (tmp_path / "proj",))
        assert f._grep_rg_executable() is None

    def test_a_world_writable_rg_is_refused(self, tmp_path, monkeypatch):
        fake = tmp_path / "rg"
        fake.write_text("#!/bin/sh\nexit 0\n")
        fake.chmod(0o777)
        monkeypatch.setattr(f.shutil, "which", lambda name: str(fake))
        monkeypatch.setattr(f, "agent_writable_roots", lambda: ())
        assert f._grep_rg_executable() is None

    def test_an_ordinary_install_is_returned_as_an_absolute_path(self, tmp_path, monkeypatch):
        fake = tmp_path / "bin" / "rg"
        fake.parent.mkdir()
        fake.write_text("#!/bin/sh\nexit 0\n")
        fake.chmod(0o755)
        monkeypatch.setattr(f.shutil, "which", lambda name: str(fake))
        monkeypatch.setattr(f, "agent_writable_roots", lambda: ())
        assert f._grep_rg_executable() == str(fake.resolve())

    def test_a_windows_install_is_screened_by_its_acl_not_by_synthesized_mode_bits(
        self, tmp_path, monkeypatch
    ):
        """Windows reports 0o666 for any writable file, so the world-writable bit is
        set on every ordinary install and is evidence of nothing -- reading it there
        would drop the whole platform to the python engine. The security descriptor
        is the real evidence, so a descriptor that clears admits the binary."""
        fake = tmp_path / "bin" / "rg.exe"
        fake.parent.mkdir()
        fake.write_text("")
        fake.chmod(0o666)
        expected = str(fake.resolve())
        _windows_rg(monkeypatch, fake)
        assert f._grep_rg_executable() == expected

    def test_a_windows_rg_the_acl_refuses_is_not_spawned(self, tmp_path, monkeypatch):
        """A descriptor naming a third-account owner, an untrusted writer, a NULL
        DACL or a remote volume is what the check exists to catch: the containment
        check alone would pass a planted rg.exe in a gateway-user-writable PATH
        directory that sits outside every agent-writable root."""
        fake = tmp_path / "bin" / "rg.exe"
        fake.parent.mkdir()
        fake.write_text("")
        _windows_rg(monkeypatch, fake, acl_error="rg can be replaced by DOMAIN\\someone")
        assert f._grep_rg_executable() is None

    def test_an_unverifiable_windows_sid_refuses_rather_than_spawning(self, tmp_path, monkeypatch):
        """Fail CLOSED: the gateway user's SID is the analog of uid, and a check that
        cannot name the principal it is deciding for has cleared nothing."""
        fake = tmp_path / "bin" / "rg.exe"
        fake.parent.mkdir()
        fake.write_text("")
        _windows_rg(monkeypatch, fake, sid="")
        assert f._grep_rg_executable() is None

    def test_a_windows_install_inside_an_agent_tree_is_still_refused(self, tmp_path, monkeypatch):
        """The containment check is the platform-neutral half, and it still runs after
        the ACL clears -- an rg the descriptor likes is refused anyway when it sits
        somewhere the agent can write."""
        planted = tmp_path / "proj" / "node_modules" / ".bin"
        planted.mkdir(parents=True)
        fake = planted / "rg.exe"
        fake.write_text("")
        _windows_rg(monkeypatch, fake, roots=(tmp_path / "proj",))
        assert f._grep_rg_executable() is None

    def test_no_rg_means_the_python_engine(self, monkeypatch):
        monkeypatch.setattr(f.shutil, "which", lambda name: None)
        assert f._grep_rg_executable() is None


class TestCachedSegmentCap:
    """Every extractor checks its running total AFTER appending, so one segment
    could be a 50 MB paragraph -- and the cache retains 64 documents."""

    def test_segments_are_cut_to_the_aggregate_ceiling(self, monkeypatch):
        monkeypatch.setattr(f, "_GREP_DOC_MAX_CHARS", 10)
        capped = f._grep_cap_segments((("p 1", "abcdef"), ("p 2", "ghijklmno"), ("p 3", "z")))
        assert capped == (("p 1", "abcdef"), ("p 2", "ghij"))
        assert sum(len(t) for _l, t in capped) == 10

    def test_a_single_oversized_segment_is_truncated(self, monkeypatch):
        monkeypatch.setattr(f, "_GREP_DOC_MAX_CHARS", 5)
        assert f._grep_cap_segments((("doc", "x" * 1000),)) == (("doc", "xxxxx"),)

    @pytest.mark.asyncio
    async def test_the_cache_never_retains_more_than_the_ceiling(self, docs, monkeypatch):
        f._GREP_DOC_CACHE.clear()
        monkeypatch.setattr(f, "_GREP_DOC_MAX_CHARS", 8)
        monkeypatch.setattr(f, "_grep_rg_executable", lambda: None)
        await _grep(docs, "widget")
        for segments in f._GREP_DOC_CACHE.values():
            assert sum(len(t) for _l, t in segments) <= 8


class TestAuditRedaction:
    @pytest.mark.asyncio
    async def test_a_secret_typed_as_the_query_never_reaches_the_audit_log(
        self, tree, mock_sel, monkeypatch
    ):
        """Previews are redacted for the pasted-secret case; a user grepping for a
        token VALUE types the token, which is the same class of text."""
        monkeypatch.setattr(f, "_grep_rg_executable", lambda: None)
        await _grep(tree, "AKIAIOSFODNN7EXAMPLE")
        for call in mock_sel.log_api_access.call_args_list:
            assert "AKIAIOSFODNN7EXAMPLE" not in str(call.kwargs.get("resources", ""))


class TestWorkbookExpansion:
    def test_a_workbook_that_expands_past_the_cap_never_reaches_openpyxl(
        self, tmp_path, monkeypatch
    ):
        """The shared inventory vet bounds member COUNT and central-directory
        size, not expansion, so a single hugely-compressed member passed it and
        reached openpyxl's XML parser."""
        pytest.importorskip("openpyxl")
        path = tmp_path / "bomb.xlsx"
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("xl/workbook.xml", "<workbook/>")
            zf.writestr("xl/big.bin", b"\0" * (2 * 1024 * 1024))
        opened: list[str] = []
        monkeypatch.setattr(f, "_SHEET_MAX_EXPANDED_BYTES", 1024)

        real_import = __import__

        def tracking_import(name, *args, **kwargs):
            if name == "openpyxl":
                opened.append(name)
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr("builtins.__import__", tracking_import)
        assert f._grep_xlsx_segments(path.read_bytes(), str(path)) == ()
        # The refusal is BEFORE the parser: reaching openpyxl and bailing out
        # later would already have paid the expansion this cap exists to refuse.
        assert opened == []

    def test_a_workbook_within_the_cap_is_still_read(self, tmp_path):
        openpyxl = pytest.importorskip("openpyxl")
        path = tmp_path / "small.xlsx"
        book = openpyxl.Workbook()
        book.active.title = "Ledger"
        book.active.append(["note"])
        book.active.append(["the WIDGET invoice"])
        book.save(path)
        segments = f._grep_xlsx_segments(path.read_bytes(), str(path))
        assert any("WIDGET" in text for _label, text in segments)


class TestHelpers:
    def test_the_sensitive_globs_are_derived_from_the_shared_fence(self):
        """Derived, never a second hand-kept list: a copy here would be the thing
        that silently stops matching what ``is_sensitive_path`` blocks."""
        from kiro_crew.security import sensitive_home_dirs

        args = f._grep_sensitive_globs()
        assert args[0::2] == ["--iglob"] * (len(args) // 2)
        patterns = set(args[1::2])
        for entry in sensitive_home_dirs():
            if entry.strip("/"):
                assert f"!**/{entry.strip('/')}" in patterns

    def test_the_argv_carries_every_flag_the_parity_claim_rests_on(self):
        """Each flag closes a divergence the fallback cannot match without it.

        Asserted on the argv rather than through behaviour so the check runs on a
        host with no ripgrep installed."""
        argv = f._grep_rg_argv("/r", "needle")
        assert "--fixed-strings" in argv  # the fallback matches a literal
        assert "--ignore-case" in argv  # the fallback folds case unconditionally
        assert "--smart-case" not in argv  # ... which --smart-case does not
        assert "--no-ignore" in argv  # os.walk cannot honour ignore files
        assert argv[argv.index("--max-count") + 1] == "1"  # one hit per file
        # The fallback skips a file over the ceiling; rg must skip it too.
        assert argv[argv.index("--max-filesize") + 1] == str(f._GREP_MAX_FILE_BYTES)
        for ext in f._GREP_DOC_EXTS:
            assert f"!**/*{ext}" in argv  # the document pass owns these

    def test_the_argv_runs_the_vetted_absolute_path_not_a_name(self):
        """The spawn must run the file that was checked. A bare ``rg`` would be
        re-resolved through $PATH at exec time -- after the check."""
        argv = f._grep_rg_argv("/r", "needle", "/opt/tools/bin/rg")
        assert argv[0] == "/opt/tools/bin/rg"

    def test_every_glob_in_the_argv_is_negated(self):
        """One non-negated glob flips ripgrep's whole glob set into ALLOWLIST mode,
        which would silently exclude every file the set does not name."""
        argv = f._grep_rg_argv("/r", "needle")
        globs = [argv[i + 1] for i, tok in enumerate(argv) if tok in ("--glob", "--iglob")]
        assert globs, "the argv should carry exclusions"
        assert all(g.startswith("!") for g in globs), [g for g in globs if not g.startswith("!")]

    def test_the_query_is_passed_after_the_argument_terminator(self):
        """A query starting with ``-`` would otherwise be read as a flag."""
        assert f._grep_rg_argv("/root", "--needle")[-3:] == ["--", "--needle", "/root"]

    def test_an_error_exit_asks_for_the_fallback_rather_than_reporting_no_matches(
        self, monkeypatch
    ):
        """rg exits 0 with matches, 1 with none and >1 on an error. Only the last
        is 'no verdict' — and under ``--no-messages`` an error's stdout is empty,
        which read as an authoritative empty result set."""
        _rg_spawn(monkeypatch, lines=(), returncode=2)
        assert f._grep_rg("/r", "needle", f.time.monotonic() + 5) is None

        # Exit 1 is a real answer: the tree genuinely holds no match.
        _rg_spawn(monkeypatch, lines=(), returncode=1)
        assert f._grep_rg("/r", "needle", f.time.monotonic() + 5) == ([], False)

    def test_a_timeout_reports_its_partial_hits_instead_of_an_empty_fallback(self, monkeypatch):
        """The deadline is SHARED, so handing a timeout to the python pass gives it
        nothing left and it returns an empty list at its first check. The matches
        ripgrep already printed are real; they come back marked truncated."""
        record = json.dumps(
            {
                "type": "match",
                "data": {
                    "path": {"text": "/r/a.py"},
                    "line_number": 4,
                    "lines": {"text": "needle here"},
                },
            }
        )

        # The records are read as they arrive, so a timeout on `wait()` finds them
        # already parsed. No bytes-vs-str decode of TimeoutExpired.stdout exists
        # to get wrong any more -- the partial answer falls out of the loop.
        _rg_spawn(monkeypatch, lines=(record + "\n",), wait_raises=True)
        hits, truncated = f._grep_rg("/r", "needle", f.time.monotonic() + 5)
        assert truncated is True
        assert [(h["file"], h["line"]) for h in hits] == [("/r/a.py", 4)]

    def test_a_fail_closed_sandbox_refusal_takes_the_python_engine_not_a_500(self, monkeypatch):
        """`SandboxUnavailableError` is a RuntimeError, raised when the host has no
        sandbox backend. Catching it is what keeps an ordinary Linux box without
        bubblewrap searching through the python engine instead of answering every
        keystroke with an HTTP 500."""
        monkeypatch.setattr(f, "_grep_rg_executable", lambda: "/usr/bin/rg")
        monkeypatch.setattr(f, "cgroup_scope_argv", lambda cmd: cmd)

        def refuses(cmd):
            raise RuntimeError("no sandbox backend and unsandboxed exec is not allowed")

        monkeypatch.setattr(f, "wrap_argv", refuses)
        assert f._grep_rg("/r", "needle", f.time.monotonic() + 5) is None

    def test_the_launcher_temp_file_is_unlinked_on_every_path(self, monkeypatch, tmp_path):
        """`wrap_argv` returns (argv, cleanup_path) and the caller owns the temp.
        Discarding it leaked one launcher file per search."""
        unlinked: list[str] = []
        monkeypatch.setattr(f.os, "unlink", lambda p: unlinked.append(p))
        monkeypatch.setattr(f, "cgroup_scope_argv", lambda cmd: cmd)
        monkeypatch.setattr(f, "_grep_rg_executable", lambda: "/usr/bin/rg")
        monkeypatch.setattr(f, "wrap_argv", lambda cmd: (cmd, "/tmp/launcher-xyz"))

        # Success path.
        _rg_spawn(monkeypatch, lines=(), returncode=0, patch_wrap=False)
        f._grep_rg("/r", "needle", f.time.monotonic() + 5)
        assert unlinked == ["/tmp/launcher-xyz"]

        # Fallback path: still unlinked.
        unlinked.clear()
        _rg_spawn(monkeypatch, lines=(), returncode=2, patch_wrap=False)
        f._grep_rg("/r", "needle", f.time.monotonic() + 5)
        assert unlinked == ["/tmp/launcher-xyz"]

    def test_the_reader_stops_at_the_cap_instead_of_draining_the_tree(self, monkeypatch):
        """`--max-count 1` bounds records per FILE and `--max-filesize` bounds one
        record, but neither bounds the record COUNT -- that is the number of matching
        files. Buffering all of them to keep the first 200 is the growth path; the
        reader stops at the cap and kills the child instead."""

        def record_for(i):
            return (
                json.dumps(
                    {
                        "type": "match",
                        "data": {
                            "path": {"text": f"/r/f{i}.py"},
                            "line_number": 1,
                            "lines": {"text": "needle"},
                        },
                    }
                )
                + "\n"
            )

        served: list[int] = []

        def endless():
            i = 0
            while True:
                served.append(i)
                yield record_for(i)
                i += 1

        killed = _rg_spawn(monkeypatch, lines=endless())
        hits, truncated = f._grep_rg("/r", "needle", f.time.monotonic() + 5)
        assert truncated is True
        assert len(hits) == f._GREP_MAX_RESULTS
        # Bounded, not unbounded: the reader thread may run ahead of the parse loop
        # by at most the hand-off queue, and BLOCKS once it is full -- so the tree
        # is never drained to find the 200 hits that are kept.
        assert len(served) <= f._GREP_MAX_RESULTS + f._GREP_RG_QUEUE_LINES + 2
        assert killed, "the child was left running after the cap"

    def test_a_capped_search_reaps_the_child_and_lets_the_reader_exit(self, monkeypatch):
        """One search runs per keystroke, so anything a capped search leaves behind
        accumulates. Killing without reaping leaves a zombie; and the reader blocks on
        a full queue once this side stops consuming -- including on the sentinel put in
        its own `finally` -- so it wedges instead of exiting."""
        import threading as _threading

        record = (
            json.dumps(
                {
                    "type": "match",
                    "data": {
                        "path": {"text": "/r/a.py"},
                        "line_number": 1,
                        "lines": {"text": "needle"},
                    },
                }
            )
            + "\n"
        )

        def endless():
            while True:
                yield record

        before = {t.name for t in _threading.enumerate()}
        made: list = []
        killed = _rg_spawn(monkeypatch, lines=endless(), made=made)
        hits, truncated = f._grep_rg("/r", "needle", f.time.monotonic() + 5)

        assert truncated is True
        assert len(hits) == f._GREP_MAX_RESULTS
        assert killed, "the child was not stopped"
        assert made, "no child was spawned"
        # Reaped, not merely signalled: an unwaited kill is a zombie per search.
        assert made[0]._reaped is True
        # And the pipe was closed, which is what ends the reader's iteration.
        assert made[0].stdout.closed is True

        deadline = f.time.monotonic() + 3
        while f.time.monotonic() < deadline:
            if "file-grep-rg" not in {t.name for t in _threading.enumerate()} - before:
                break
            f.time.sleep(0.05)
        leaked = {t.name for t in _threading.enumerate()} - before
        assert "file-grep-rg" not in leaked, f"reader thread survived: {leaked}"

    def test_a_query_that_prints_nothing_still_answers_inside_the_budget(self, monkeypatch):
        """rg emits no record for a non-matching file, so a rare-or-absent query over
        a large tree prints NOTHING while it traverses. Reading the pipe inline could
        only notice the deadline when a line arrived, so this case ran to rg's own
        completion and then reported `truncated: False` -- a budget overrun presented
        as a complete answer, with a transfer-pool worker held for the whole walk."""
        import threading as _threading

        release = _threading.Event()

        class _Silent:
            """A pipe that never yields a line until the test lets it go."""

            def __iter__(self):
                return self

            def __next__(self):
                release.wait(30)
                raise StopIteration

        killed = _rg_spawn(monkeypatch, lines=_Silent())
        started = f.time.monotonic()
        hits, truncated = f._grep_rg("/r", "needle", started + 0.3)
        elapsed = f.time.monotonic() - started
        release.set()
        assert hits == []
        # Short, and SAYS it is short -- the two facts the budget contract rests on.
        assert truncated is True
        assert elapsed < 5, f"the read was not deadline-bounded ({elapsed:.1f}s)"
        assert killed, "the child was left traversing after the deadline"

    def test_a_failed_ripgrep_asks_for_the_fallback_rather_than_claiming_no_matches(
        self, monkeypatch
    ):
        """None, not an empty list: a missing binary and a failed spawn are both
        'no verdict', and reporting them as an empty result set would say 'no
        matches' about a search that never ran. A TIMEOUT is not in that class —
        it has partial hits, and the test above pins them."""

        def boom(argv, **kwargs):
            raise OSError("no rg")

        monkeypatch.setattr(f, "_grep_rg_executable", lambda: "/usr/bin/rg")
        monkeypatch.setattr(f, "wrap_argv", lambda cmd: (cmd, None))
        monkeypatch.setattr(f, "cgroup_scope_argv", lambda cmd: cmd)
        monkeypatch.setattr(f, "popen_limited", boom)
        assert f._grep_rg("/r", "needle", f.time.monotonic() + 5) is None

        monkeypatch.setattr(f, "_grep_rg_executable", lambda: None)
        assert f._grep_rg("/r", "needle", f.time.monotonic() + 5) is None


class TestAudit:
    @pytest.mark.asyncio
    async def test_an_allowed_search_is_audited_with_its_engine_and_counts(self, tree, mock_sel):
        await _grep(tree, "needle")
        kwargs = mock_sel.log_api_access.call_args.kwargs
        assert kwargs["operation"] == "file_grep"
        assert kwargs["outcome"] == "allowed"
        assert "engine=" in kwargs["resources"]
        assert "results=" in kwargs["resources"]
