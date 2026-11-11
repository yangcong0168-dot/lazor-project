# Bundled third-party components

The platform-specific complete release ZIPs include all Python dependencies
installed from `requirements.txt` in `.deps/`. The current runtime dependency is
`z3-solver==5.3.3.0`; its Python modules and native Z3 library are included together.
The dependency manifest is `.deps/lazor_bundle.json`. Original package metadata,
headers, libraries and any notices supplied in the wheel are retained.

Z3 is copyright Microsoft Corporation and distributed under the MIT License.
See `third_party/z3/LICENSE.txt`, copied from the upstream source revision
`430d19e697e2ec9bad79ff210d41b4a7a5fcfa3e` associated with this package version.
Upstream: <https://github.com/Z3Prover/z3>.
Distribution: <https://pypi.org/project/z3-solver/5.3.3.0/>.

The official Windows wheel also supplies Microsoft Visual C++ runtime DLLs.
These remain Microsoft components subject to their applicable redistribution
terms; the Z3 MIT License does not relicense those runtime DLLs. They are included
unchanged as part of the official wheel installation.
Microsoft's runtime license terms, also linked by the Z3 project:
<https://visualstudio.microsoft.com/license-terms/vs2022-cruntime/>.

Python, its standard library and Tk are provided by the user's Python
installation and are not bundled in these ZIPs. The project's own simulation,
editor and database modules otherwise use the Python standard library.
