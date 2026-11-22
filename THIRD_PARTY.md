# Third-party software

Sockseek and Nicotine+ are external programs and are not included in this repository. Listening Room invokes them when configured by the host. Sockseek is the current download adapter, the Soulseek search and download CLI; it is still a separate project, but the package routes fetch its pinned release and verify the checksum as part of installation, and a from-source install provisions it the same way with `scripts/provision_sockseek.py`. Nicotine+ is used only for optional sharing back to Soulseek and is never provisioned.

The React frontend in `frontend/react/` (plan 042) builds on these packages, installed with yarn and recorded in `frontend/react/yarn.lock`:

- react (MIT License)
- react-dom (MIT License)
- @ui-sdk/react (Apache License 2.0)
- @ui-sdk/core (Apache License 2.0)

## Figtree

The browser bundles Figtree Variable by Erik Kennedy under the SIL Open Font License 1.1. The license is included with the browser assets. [Figtree license](frontend/react/public/licenses/Figtree-OFL.txt)

The landing page uses original generated sleeve artwork for First light and a preview of the project’s CC0 demo composition. The tour bundles both locally. Recording credit examples link to their source.
