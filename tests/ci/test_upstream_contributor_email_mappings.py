"""Every contributor mapping added by upstream merge 0965ced2a6 resolves."""
from __future__ import annotations

import pytest

from scripts.releases import authors

UPSTREAM_MAPPINGS = [
    ("156911559@qq.com", "fengyufuyao"),
    ("RegeneratusLabs@users.noreply.github.com", "RegeneratusLabs"),
    ("bots@hermesmade.lol", "Noshkoto"),
    ("dev.vilfer@gmail.com", "TheVilfer"),
    ("dev.vlad.pohorielov@gmail.com", "vladyslavpatrick"),
    ("developer@skeptical.agency", "skeptical-agency"),
    ("explorelifehus@gmail.com", "explorelifehus-eng"),
    ("familiarcow@proton.me", "familiarcow"),
    ("josh@nutri-tech.com.au", "RegeneratusLabs"),
    ("josht@nutri-tech.com.au", "RegeneratusLabs"),
    ("julian@saint.work", "julian-saint-work"),
    ("kiter2509@gmail.com", "memeshee"),
    ("mgibbs@surestepsi.com", "MikeGibbsOnyx"),
    ("noprijbi99@gmail.com", "noprijbi99-dotcom"),
    ("noshkoto@users.noreply.github.com", "Noshkoto"),
    ("ops@teamshift.io", "enckequity"),
    ("ramosespinosamarcos@gmail.com", "ddaikodaiko"),
    ("root@localhost.localdomain", "Justnevo"),
    ("saltprophetsound@gmail.com", "SaltProphet"),
    ("sebastian@wagnersolutionsai.com", "SebaWag"),
    ("surajkumarsyn479@gmail.com", "Surajkumar5050"),
    ("vcruz305@users.noreply.github.com", "vcruz305"),
]


@pytest.mark.parametrize(("email", "login"), UPSTREAM_MAPPINGS)
def test_new_mapping_loads_to_intended_login(email: str, login: str) -> None:
    assert authors.AUTHOR_MAP[email] == login
    assert authors.resolve_author("unmapped author name", email) == f"@{login}"
