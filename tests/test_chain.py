"""chain.py が、キーを OpenAlex の ID・DOI・題名のどれと見なすかを検査する。"""

import contextlib
import importlib.util
import io
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "skills/imozuru/scripts/chain.py"
SPEC = importlib.util.spec_from_file_location("imozuru_chain", SCRIPT)
chain = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(chain)

WORK = {"id": "https://openalex.org/W2073651707", "display_name": "Weak Bourbaki unmixed rings",
        "publication_year": 2004, "cited_by_count": 10, "doi": None, "best_oa_location": None}


class KeyKindTests(unittest.TestCase):
    """「W」で始まる題名を ID と取り違えない（イシュー #7）。"""

    def paths(self, *argv):
        seen = []

        def get(path, **params):
            seen.append(path)
            return WORK if path.startswith("/works/") else {"results": [WORK]}

        with patch.object(chain, "get", side_effect=get), \
                patch.object(sys, "argv", ["chain.py", *argv]), \
                contextlib.redirect_stdout(io.StringIO()):
            chain.main()
        return seen

    def test_title_starting_with_w_is_searched(self):
        self.assertEqual(self.paths("find", "Weak", "Bourbaki", "unmixed", "rings"), ["/works"])
        with patch.object(chain, "get", return_value={"results": [WORK]}) as get:
            self.assertEqual(chain.resolve("What is exploratory search"), WORK)
        self.assertEqual(get.call_args.args[0], "/works")

    def test_work_id_is_resolved_directly(self):
        self.assertEqual(self.paths("find", "W2073651707"), ["/works/W2073651707"])
        self.assertEqual(self.paths("find", "https://openalex.org/W2073651707"), ["/works/W2073651707"])

    def test_doi_is_resolved_directly(self):
        self.assertEqual(self.paths("find", "10.1016/j.jalgebra.2004.01.001"),
                         ["/works/doi:10.1016/j.jalgebra.2004.01.001"])


if __name__ == "__main__":
    unittest.main()
