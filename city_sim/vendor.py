"""Install the browser libraries locally, verifying pinned NPM archives."""
import argparse
from hashlib import sha256
from http.client import IncompleteRead
from io import BytesIO
from pathlib import Path
from tarfile import open as open_tar
from urllib.error import URLError
from urllib.request import urlopen

PACKAGES = {
    "maplibre": ("maplibre-gl", "5.6.2",
                 "86ed7f726a7df7b7539d535fc3fa276c1ff845caab962f1e67e4c5dc6152d7f6",
                 {"dist/maplibre-gl.js": "maplibre-gl.js", "dist/maplibre-gl.css": "maplibre-gl.css",
                  "dist/LICENSE.txt": "maplibre-LICENSE.txt"}),
    "three": ("three", "0.179.1",
              "8384b0c13ced6dd3f9f0f3beba730f94c8c6a4774e04185e08076407c490a3d9",
              {"build/three.module.min.js": "three.module.min.js",
               "build/three.core.min.js": "three.core.min.js", "LICENSE": "three-LICENSE.txt"}),
}


def download(url):
    for attempt in range(3):
        try:
            with urlopen(url, timeout=45) as response:
                return response.read()
        except (IncompleteRead, URLError, TimeoutError):
            if attempt == 2:
                raise


def install(cache):
    target = Path(__file__).parent / "web" / "vendor"
    target.mkdir(parents=True, exist_ok=True)
    for key, (package, version, expected, paths) in PACKAGES.items():
        if cache:
            data = (cache / f"{key}.tgz").read_bytes()
        else:
            data = download(f"https://registry.npmjs.org/{package}/-/{package}-{version}.tgz")
        if sha256(data).hexdigest() != expected:
            raise ValueError(f"Archive checksum mismatch: {package}")
        with open_tar(fileobj=BytesIO(data), mode="r:gz") as tar:
            for source, destination in paths.items():
                (target / destination).write_bytes(tar.extractfile(f"package/{source}").read())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path)
    install(parser.parse_args().cache)
