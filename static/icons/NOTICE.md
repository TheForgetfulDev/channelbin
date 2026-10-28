# Material Design Icons (vendored as a sprite)

- Project: https://pictogrammers.com/library/mdi/ (npm package `@mdi/js`)
- Version: 7.4.47, pinned exactly in `package.json`
- License: Pictogrammers Free License; the icons are Apache License 2.0. The full text is in
  `LICENSE` beside this file, copied unmodified from the package.

`mdi-sprite.svg` holds only the icons the app's registry (`app/icons.py`) names, one
`<symbol id="i-<meaning>">` per meaning, with each path copied unmodified from the package by
`tools/build_icons.py`. It is generated: never edit it by hand.

Upgrading: change the pin in `package.json`, `npm install`, run `python3 tools/build_icons.py`,
copy the new package's `LICENSE` here and update the version above.
`tests/test_icon_registry.py` fails until the sprite matches the installed package.
