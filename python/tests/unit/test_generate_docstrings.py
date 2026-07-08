from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


def _load_generate_docstrings_module():
    module_path = Path(__file__).resolve().parents[2] / "generate_docstrings.py"
    module_name = "generate_docstrings_module"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None
    assert spec.loader is not None
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


generate_docstrings = _load_generate_docstrings_module()


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("#[new]", True),
        ("#[allow(unused_imports)]", True),
        ("#[allow(unused_imports)] // Used in template pattern", True),
        (")]", True),
        (")] // trailing comment", True),
        ("#[cfg_attr(", False),
        ('feature = "python",', False),
    ],
)
def test_attr_end_re_matches_closing_attribute_lines(line, expected):
    # Act
    result = generate_docstrings.ATTR_END_RE.search(line) is not None

    # Assert
    assert result is expected


def test_parse_pyo3_items_tolerates_attribute_with_trailing_comment():
    # Arrange: the trailing comment must not swallow the `#[pymethods]` marker
    lines = [
        "#[allow(unused_imports)] // Used in template pattern",
        "use nautilus_core::UnixNanos;",
        "",
        "#[pymethods]",
        "impl LongRatio {",
        "    #[new]",
        "    fn py_new() -> Self {",
        "        Self::new()",
        "    }",
        "}",
    ]

    # Act
    items = generate_docstrings.parse_pyo3_items(lines)

    # Assert
    assert len(items) == 1
    assert items[0]["fn_name"] == "py_new"
    assert items[0]["impl_type"] == "LongRatio"
    assert items[0]["is_constructor"] is True
    assert items[0]["in_pymethods"] is True


def test_collect_source_docs_attaches_doc_across_commented_attribute(tmp_path):
    # Arrange: the struct follows the commented attribute directly, so treating
    # the attribute as multi-line would swallow the struct and lose the doc
    source = """\
/// Calculates the thing.
///
/// More detail.
#[allow(dead_code)] // trailing comment
pub struct Foo {}

#[cfg_attr(
    feature = "python",
    pyo3::pyclass(module = "nautilus_trader")
)] // trailing comment on closing line
pub struct Bar {}
"""
    (tmp_path / "lib.rs").write_text(source)

    # Act
    docs = generate_docstrings.collect_source_docs(tmp_path)

    # Assert
    assert docs[(None, "Foo")] == ["Calculates the thing.", "", "More detail."]
    assert (None, "Bar") not in docs  # No doc comment on Bar
