import { Component, type ErrorInfo, type ReactNode } from "react";

type Props = {
  children: ReactNode;
};

type State = {
  error: Error | null;
};

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, errorInfo: ErrorInfo) {
    console.error("Dashboard render error", error, errorInfo);
  }

  render() {
    if (!this.state.error) return this.props.children;

    return (
      <div className="min-h-screen bg-background p-6 text-foreground">
        <div className="mx-auto max-w-3xl border border-destructive/40 bg-destructive/[0.04] p-4">
          <h1 className="mb-2 font-display text-lg tracking-wider">{"Dashboard Render Error"}</h1>
          <pre className="max-h-[70vh] overflow-auto whitespace-pre-wrap break-words font-mono text-xs text-destructive">
            {this.state.error.stack || this.state.error.message}
          </pre>
        </div>
      </div>
    );
  }
}
