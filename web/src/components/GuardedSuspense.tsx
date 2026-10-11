import { Suspense, type ReactNode } from "react";
import { useLocation } from "react-router";

import { ErrorBoundary } from "@/components/ErrorBoundary";
import { useI18n } from "@/i18n";

interface GuardedSuspenseProps {
  fallback: ReactNode;
  children: ReactNode;
}

/** A route ``Suspense`` behind the dashboard's error boundary: one crashing page shows a localized,
 *  recoverable fallback instead of blanking the shell, and navigating away resets it. */
export function GuardedSuspense({ fallback, children }: GuardedSuspenseProps) {
  const { t } = useI18n();
  const { pathname } = useLocation();
  return (
    <ErrorBoundary
      resetKeys={[pathname]}
      title={t.common.errorBoundaryTitle}
      description={t.common.errorBoundaryDescription}
      retryLabel={t.common.retry}
      reloadLabel={t.common.refresh}
    >
      <Suspense fallback={fallback}>{children}</Suspense>
    </ErrorBoundary>
  );
}
