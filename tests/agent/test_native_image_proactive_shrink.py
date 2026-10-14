"""``agent.native_image_max_base64_bytes`` proactively downscales big native image attachments.

Upstream attaches images at native size and shrinks only after a provider rejects. The fork's IM
deployment opted into a cap so a phone screenshot doesn't make every image turn slow; 0 (the
default) keeps upstream behaviour byte-for-byte.
"""

import os

import pytest
import yaml

from hermes_constants import get_hermes_home

Image = pytest.importorskip("PIL.Image")


def _noisy_png(path, side=900):
    Image.frombytes("RGB", (side, side), os.urandom(side * side * 3)).save(path, format="PNG")
    return path


def _set_cap(value):
    (get_hermes_home() / "config.yaml").write_text(yaml.safe_dump({"agent": {"native_image_max_base64_bytes": value}}))


def test_cap_downscales_an_oversized_attachment(tmp_path):
    from agent.image_routing import _file_to_data_url

    image = _noisy_png(tmp_path / "shot.png")
    original_b64 = len(image.read_bytes()) * 4 // 3
    _set_cap(400_000)
    assert original_b64 > 400_000

    data_url = _file_to_data_url(image)

    assert data_url.startswith("data:image/jpeg;base64,")
    assert len(data_url) < original_b64


def test_default_zero_keeps_native_size(tmp_path):
    from agent.image_routing import _file_to_data_url

    image = _noisy_png(tmp_path / "shot.png")
    _set_cap(0)

    data_url = _file_to_data_url(image)

    assert data_url.startswith("data:image/png;base64,")
    assert len(data_url.split(",", 1)[1]) == (len(image.read_bytes()) + 2) // 3 * 4
