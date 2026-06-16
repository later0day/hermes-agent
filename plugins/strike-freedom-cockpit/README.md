# Strike Freedom Cockpit

Dashboard skin demo for the Hermes plugin slot and theme systems.

This plugin does not add agent tools or backend runtime behavior. It is a
visual dashboard extension that pairs a cockpit-style theme with hidden slot
content:

- `theme/strike-freedom.yaml` defines the palette, typography, cockpit layout,
  component chrome, scanlines, and optional art asset hooks.
- `dashboard/manifest.json` registers a hidden dashboard plugin.
- `dashboard/dist/index.js` fills the `sidebar`, `header-left`, and
  `footer-right` shell slots when the active theme uses `layoutVariant:
  cockpit`.

## What It Adds

- A Strike Freedom style dashboard theme with deep navy, cyan, and gold HUD
  colors.
- A cockpit side rail with `MS-STATUS`, `ENERGY`, `SHIELD`, and `POWER` bars.
- A header crest slot that can use `assets.crest` from the active theme.
- A footer-right cockpit tagline.

The telemetry is dashboard status decoration, not hardware telemetry. It maps
existing `/api/status` fields into HUD-style bars:

- `ENERGY` comes from whether the gateway is running.
- `SHIELD` comes from the number of connected gateway platforms.
- `POWER` comes from the active session count.

## Install The Theme

The dashboard plugin is auto-discovered when this directory is shipped in
`plugins/`. The theme YAML is intentionally kept as a user-installable demo.

```bash
mkdir -p ~/.hermes/dashboard-themes
cp plugins/strike-freedom-cockpit/theme/strike-freedom.yaml ~/.hermes/dashboard-themes/
```

Restart the dashboard or call `/api/dashboard/plugins/rescan`, then choose
`Strike Freedom` from the dashboard theme switcher.

## Custom Artwork

The plugin reads CSS variables generated from the active theme:

```yaml
assets:
  hero: "/my-images/strike-freedom.png"
  crest: "/my-images/compass-crest.svg"
  bg: "/my-images/cosmic-era-bg.jpg"
```

If `hero` or `crest` are empty, the plugin shows a placeholder hero panel and a
small fallback SVG crest.
