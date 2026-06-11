/**
 * Strike Freedom Cockpit dashboard plugin.
 *
 * Slot-only plugin. It stays hidden from the nav and populates cockpit-only
 * shell slots when the active dashboard theme uses layoutVariant: cockpit.
 */
(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  const PLUGINS = window.__HERMES_PLUGINS__;
  if (!SDK || !PLUGINS || !PLUGINS.registerSlot) {
    return;
  }

  const { React } = SDK;
  const { useEffect, useState } = SDK.hooks;
  const { api } = SDK;

  function cssVar(name) {
    if (typeof document === "undefined") return "";
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  function currentLayoutVariant() {
    if (typeof document === "undefined") return "standard";
    return document.documentElement.dataset.layoutVariant || "standard";
  }

  function useLayoutVariant() {
    const [variant, setVariant] = useState(currentLayoutVariant);

    useEffect(function () {
      if (typeof document === "undefined" || typeof MutationObserver === "undefined") {
        return undefined;
      }
      const root = document.documentElement;
      const observer = new MutationObserver(function () {
        setVariant(currentLayoutVariant());
      });
      observer.observe(root, { attributes: true, attributeFilter: ["data-layout-variant"] });
      return function () {
        observer.disconnect();
      };
    }, []);

    return variant;
  }

  function TelemetryBar(props) {
    const { label, value, color } = props;
    const cells = [];
    const filledCells = Math.round(value / 10);

    for (let i = 0; i < 10; i += 1) {
      cells.push(
        React.createElement("span", {
          key: i,
          style: {
            flex: 1,
            height: 8,
            background: filledCells > i ? color : "rgba(255,255,255,0.06)",
            clipPath: "polygon(2px 0, 100% 0, calc(100% - 2px) 100%, 0 100%)",
            transition: "background 200ms",
          },
        }),
      );
    }

    return React.createElement(
      "div",
      { style: { display: "flex", flexDirection: "column", gap: 4 } },
      React.createElement(
        "div",
        {
          style: {
            display: "flex",
            justifyContent: "space-between",
            fontSize: "0.65rem",
            letterSpacing: "0.12em",
            opacity: 0.75,
          },
        },
        React.createElement("span", null, label),
        React.createElement("span", { style: { color, fontWeight: 700 } }, value + "%"),
      ),
      React.createElement("div", { style: { display: "flex", gap: 2 } }, cells),
    );
  }

  function useDashboardStatus() {
    const [status, setStatus] = useState(null);

    useEffect(function () {
      let cancelled = false;

      function refresh() {
        api
          .getStatus()
          .then(function (next) {
            if (!cancelled) setStatus(next);
          })
          .catch(function () {});
      }

      refresh();
      const timer = window.setInterval(refresh, 15000);
      return function () {
        cancelled = true;
        window.clearInterval(timer);
      };
    }, []);

    return status;
  }

  function SidebarSlot() {
    const status = useDashboardStatus();
    const gatewayRunning = Boolean(status && status.gateway_running);
    const platforms = status && status.gateway_platforms ? Object.values(status.gateway_platforms) : [];
    const connectedPlatforms = platforms.filter(function (p) {
      return p && (p.state === "connected" || p.state === "running");
    }).length;
    const activeSessions =
      status && Number.isFinite(status.active_sessions) ? status.active_sessions : 0;

    const energy = gatewayRunning ? 92 : 18;
    const shield = status && status.gateway_platforms
      ? Math.min(100, 40 + connectedPlatforms * 15)
      : 70;
    const power = status ? Math.min(100, 55 + activeSessions * 10) : 87;
    const hero = cssVar("--theme-asset-hero");

    return React.createElement(
      "div",
      {
        style: {
          padding: "1rem 0.75rem",
          display: "flex",
          flexDirection: "column",
          gap: "1rem",
          fontFamily: "var(--theme-font-display, sans-serif)",
          letterSpacing: "0.08em",
          textTransform: "uppercase",
          fontSize: "0.65rem",
          minHeight: "100%",
        },
      },
      React.createElement(
        "div",
        {
          style: {
            borderBottom: "1px solid rgba(64,200,255,0.3)",
            paddingBottom: 8,
            display: "flex",
            flexDirection: "column",
            gap: 2,
          },
        },
        React.createElement("span", { style: { opacity: 0.6 } }, "ms status"),
        React.createElement("span", { style: { fontWeight: 700, fontSize: "0.85rem" } }, "zgmf-x20a"),
        React.createElement("span", { style: { opacity: 0.6, fontSize: "0.6rem" } }, "strike freedom"),
      ),
      hero
        ? React.createElement("div", {
            style: {
              width: "100%",
              aspectRatio: "3 / 4",
              backgroundImage: hero,
              backgroundSize: "contain",
              backgroundPosition: "center",
              backgroundRepeat: "no-repeat",
              opacity: 0.85,
            },
            "aria-hidden": true,
          })
        : React.createElement(
            "div",
            {
              style: {
                width: "100%",
                aspectRatio: "3 / 4",
                border: "1px dashed rgba(64,200,255,0.25)",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                fontSize: "0.55rem",
                opacity: 0.4,
                textAlign: "center",
                padding: 12,
              },
            },
            "hero slot - set assets.hero in theme",
          ),
      React.createElement(
        "div",
        {
          style: {
            borderTop: "1px solid rgba(64,200,255,0.18)",
            borderBottom: "1px solid rgba(64,200,255,0.18)",
            padding: "8px 0",
            display: "flex",
            flexDirection: "column",
            gap: 2,
          },
        },
        React.createElement("span", { style: { opacity: 0.5, fontSize: "0.55rem" } }, "pilot"),
        React.createElement("span", { style: { fontWeight: 700 } }, "hermes agent"),
        React.createElement("span", { style: { opacity: 0.5, fontSize: "0.55rem" } }, "compass"),
      ),
      React.createElement(TelemetryBar, { label: "energy", value: energy, color: "#ffce3a" }),
      React.createElement(TelemetryBar, { label: "shield", value: shield, color: "#3fd3ff" }),
      React.createElement(TelemetryBar, { label: "power", value: power, color: "#ff3a5e" }),
      React.createElement(
        "div",
        {
          style: {
            marginTop: 4,
            padding: "6px 8px",
            border: gatewayRunning
              ? "1px solid rgba(74,222,128,0.4)"
              : "1px solid rgba(255,58,94,0.4)",
            color: gatewayRunning ? "#4ade80" : "#ff3a5e",
            textAlign: "center",
            fontWeight: 700,
            fontSize: "0.6rem",
          },
        },
        gatewayRunning ? "system online" : "system offline",
      ),
    );
  }

  function HeaderCrestSlot() {
    const variant = useLayoutVariant();
    if (variant !== "cockpit") return null;

    const crest = cssVar("--theme-asset-crest");
    const inner = crest
      ? React.createElement("div", {
          style: {
            width: 28,
            height: 28,
            backgroundImage: crest,
            backgroundSize: "contain",
            backgroundPosition: "center",
            backgroundRepeat: "no-repeat",
          },
          "aria-hidden": true,
        })
      : React.createElement(
          "svg",
          {
            width: 28,
            height: 28,
            viewBox: "0 0 28 28",
            fill: "none",
            stroke: "currentColor",
            strokeWidth: 1.5,
            "aria-hidden": true,
          },
          React.createElement("path", { d: "M14 2 L26 14 L14 26 L2 14 Z" }),
          React.createElement("path", { d: "M14 8 L20 14 L14 20 L8 14 Z" }),
          React.createElement("circle", { cx: 14, cy: 14, r: 2, fill: "currentColor" }),
        );

    return React.createElement(
      "div",
      {
        style: {
          display: "flex",
          alignItems: "center",
          paddingLeft: 12,
          paddingRight: 8,
          color: "var(--color-accent, #3fd3ff)",
        },
      },
      inner,
    );
  }

  function FooterTaglineSlot() {
    return React.createElement(
      "span",
      {
        style: {
          fontFamily: "var(--theme-font-display, sans-serif)",
          fontSize: "0.6rem",
          letterSpacing: "0.18em",
          textTransform: "uppercase",
          opacity: 0.75,
          mixBlendMode: "plus-lighter",
          color: "var(--color-accent, #3fd3ff)",
          whiteSpace: "nowrap",
        },
      },
      "compass hermes systems / cosmic era 71",
    );
  }

  function HiddenPage() {
    return React.createElement(
      "div",
      { style: { padding: "2rem", opacity: 0.6, fontSize: "0.8rem" } },
      "Strike Freedom cockpit is a slot-only plugin. It fills the sidebar, header, and footer slots instead of adding a visible tab.",
    );
  }

  const NAME = "strike-freedom-cockpit";
  PLUGINS.register(NAME, HiddenPage);
  PLUGINS.registerSlot(NAME, "sidebar", SidebarSlot);
  PLUGINS.registerSlot(NAME, "header-left", HeaderCrestSlot);
  PLUGINS.registerSlot(NAME, "footer-right", FooterTaglineSlot);
})();
