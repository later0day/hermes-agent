/**
 * Example Dashboard Plugin
 *
 * Demonstrates how to build a dashboard plugin using the Hermes Plugin SDK.
 * No build step is needed; this file is a plain IIFE using dashboard globals.
 */
(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  const PLUGINS = window.__HERMES_PLUGINS__;
  if (!SDK || !PLUGINS || !PLUGINS.register || !PLUGINS.registerSlot) {
    return;
  }

  const { React } = SDK;
  const { Card, CardHeader, CardTitle, CardContent, Badge, Button } = SDK.components;
  const { useState } = SDK.hooks;
  const { cn } = SDK.utils;

  function ExamplePage() {
    const [greeting, setGreeting] = useState(null);
    const [loading, setLoading] = useState(false);

    function fetchGreeting() {
      setLoading(true);
      SDK.fetchJSON("/api/plugins/example/hello")
        .then(function (data) {
          setGreeting(data.message);
        })
        .catch(function () {
          setGreeting("(backend not available)");
        })
        .finally(function () {
          setLoading(false);
        });
    }

    return React.createElement(
      "div",
      { className: "flex flex-col gap-6" },
      React.createElement(
        Card,
        null,
        React.createElement(
          CardHeader,
          null,
          React.createElement(
            "div",
            { className: "flex items-center gap-3" },
            React.createElement(CardTitle, { className: "text-lg" }, "Example Plugin"),
            React.createElement(Badge, { variant: "outline" }, "v1.0.0"),
          ),
        ),
        React.createElement(
          CardContent,
          { className: "flex flex-col gap-4" },
          React.createElement(
            "p",
            { className: "text-sm text-muted-foreground" },
            "This is an example dashboard plugin. It demonstrates custom tabs, ",
            "backend API calls, and Hermes UI components through the Plugin SDK.",
          ),
          React.createElement(
            "div",
            { className: "flex items-center gap-3" },
            React.createElement(
              Button,
              {
                onClick: fetchGreeting,
                disabled: loading,
                className: cn(
                  "inline-flex items-center gap-2 border border-border bg-background/40 px-4 py-2",
                  "text-sm font-courier transition-colors hover:bg-foreground/10 cursor-pointer",
                ),
              },
              loading ? "Loading..." : "Call Backend API",
            ),
            greeting &&
              React.createElement(
                "span",
                { className: "text-sm font-courier text-muted-foreground" },
                greeting,
              ),
          ),
        ),
      ),
      React.createElement(
        Card,
        null,
        React.createElement(
          CardHeader,
          null,
          React.createElement(CardTitle, { className: "text-base" }, "Plugin SDK Reference"),
        ),
        React.createElement(
          CardContent,
          null,
          React.createElement(
            "div",
            { className: "grid gap-3 text-sm" },
            React.createElement(SdkReferenceRow, {
              name: "window.__HERMES_PLUGIN_SDK__.React",
              description: "React instance. Use it instead of bundling another React copy.",
            }),
            React.createElement(SdkReferenceRow, {
              name: "window.__HERMES_PLUGIN_SDK__.hooks",
              description: "React hooks exposed by the dashboard host.",
            }),
            React.createElement(SdkReferenceRow, {
              name: "window.__HERMES_PLUGIN_SDK__.components",
              description: "Shared dashboard UI primitives such as Card, Badge, Button, and Input.",
            }),
            React.createElement(SdkReferenceRow, {
              name: "window.__HERMES_PLUGIN_SDK__.api",
              description: "Hermes API client helpers such as getStatus() and getSessions().",
            }),
            React.createElement(SdkReferenceRow, {
              name: "window.__HERMES_PLUGIN_SDK__.fetchJSON",
              description: "Raw authenticated fetch helper for plugin-specific endpoints.",
            }),
          ),
        ),
      ),
    );
  }

  function SdkReferenceRow(props) {
    return React.createElement(
      "div",
      { className: "flex flex-col gap-1 border border-border p-3" },
      React.createElement("span", { className: "font-medium" }, props.name),
      React.createElement(
        "span",
        { className: "text-muted-foreground text-xs" },
        props.description,
      ),
    );
  }

  function SessionsTopBanner() {
    return React.createElement(
      Card,
      { className: "border-dashed" },
      React.createElement(
        CardContent,
        { className: "flex items-center gap-3 py-2" },
        React.createElement(Badge, { variant: "outline" }, "Example"),
        React.createElement(
          "span",
          { className: "text-xs text-muted-foreground" },
          "This banner was injected into the Sessions page by the example plugin via the ",
          React.createElement("code", { className: "font-courier" }, "sessions:top"),
          " slot.",
        ),
      ),
    );
  }

  PLUGINS.register("example", ExamplePage);
  PLUGINS.registerSlot("example", "sessions:top", SessionsTopBanner);
})();
