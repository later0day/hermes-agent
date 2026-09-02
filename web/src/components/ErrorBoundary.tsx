import { Component, type ErrorInfo, type ReactNode } from "react";
import { AlertTriangle, RotateCw } from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";

/**
 * Global React error boundary.
 *
 * A render/lifecycle exception anywhere in the subtree would otherwise
 * unmount the whole React tree and leave the dashboard a blank white page
 * with the real error visible only in the console. This boundary catches it,
 * logs it, and paints a recoverable fallback so one broken page (or a
 * transient chunk-load failure) can't take the entire dashboard down.
 *
 * It is intentionally self-contained — no i18n hook, no data fetching, no
 * app context — because those are exactly the things that may already be
 * failing when we land here. Copy is plain English with optional overrides.
 *
 * ``resetKeys`` lets a parent clear the error automatically when navigation
 * context changes (App passes the route pathname): after a crash on /skills,
 * clicking away to /sessions re-renders children fresh instead of stranding
 * the user on the fallback.
 */
interface ErrorBoundaryProps {
  children: ReactNode;
  /** When any entry changes between renders, the boundary resets itself. */
  resetKeys?: ReadonlyArray<unknown>;
  /** Optional localized copy; falls back to English literals when unset. */
  title?: string;
  description?: string;
  retryLabel?: string;
  reloadLabel?: string;
}

interface ErrorBoundaryState {
  error: Error | null;
}

function keysChanged(
  a: ReadonlyArray<unknown> | undefined,
  b: ReadonlyArray<unknown> | undefined,
): boolean {
  if (a === b) return false;
  if (!a || !b || a.length !== b.length) return true;
  for (let i = 0; i < a.length; i++) {
    if (!Object.is(a[i], b[i])) return true;
  }
  return false;
}

export class ErrorBoundary extends Component<
  ErrorBoundaryProps,
  ErrorBoundaryState
> {
  state: ErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error };
  }

  componentDidUpdate(prevProps: ErrorBoundaryProps): void {
    // Auto-recover when the parent's reset keys change (e.g. route change),
    // but only while an error is actually being shown.
    if (
      this.state.error !== null &&
      keysChanged(prevProps.resetKeys, this.props.resetKeys)
    ) {
      this.setState({ error: null });
    }
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // Surface the full error + component stack for triage. The fallback UI
    // deliberately hides the raw message from end users.
    // eslint-disable-next-line no-console
    console.error("Dashboard ErrorBoundary caught an error:", error, info);
  }

  private handleRetry = (): void => {
    this.setState({ error: null });
  };

  private handleReload = (): void => {
    window.location.reload();
  };

  render(): ReactNode {
    if (this.state.error === null) {
      return this.props.children;
    }

    const {
      title = "Something went wrong",
      description = "This view hit an unexpected error. You can try again, or reload the dashboard.",
      retryLabel = "Try again",
      reloadLabel = "Reload",
    } = this.props;

    return (
      <div
        role="alert"
        className="flex min-h-[16rem] flex-1 flex-col items-center justify-center gap-4 p-6 text-center"
      >
        <div className="flex size-12 items-center justify-center rounded-full bg-destructive/10 text-destructive">
          <AlertTriangle className="size-6" aria-hidden="true" />
        </div>
        <div className="space-y-1">
          <h2 className="text-base font-semibold text-foreground">{title}</h2>
          <p className="max-w-md text-sm text-muted-foreground">{description}</p>
        </div>
        <div className="flex items-center gap-2">
          <Button
            outlined
            size="sm"
            prefix={<RotateCw className="size-4" aria-hidden="true" />}
            onClick={this.handleRetry}
          >
            {retryLabel}
          </Button>
          <Button size="sm" onClick={this.handleReload}>
            {reloadLabel}
          </Button>
        </div>
      </div>
    );
  }
}

export default ErrorBoundary;
