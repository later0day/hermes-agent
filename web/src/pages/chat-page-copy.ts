import { useEffect, useMemo, useRef } from "react";

import type { Translations } from "@/i18n";
import { localeSection } from "@/i18n/section";
import {
  PTY_GAVE_UP_BANNER, PTY_RECONNECTING_BANNER, PTY_SESSION_ENDED_MESSAGE, PTY_START_FAILED_MESSAGE,
  PTY_TOKEN_MISSING_BANNER, ptyRejectionBanner,
} from "@/lib/pty-close-copy";
import { PTY_RESUME_LOADING_MESSAGE } from "@/lib/pty-resume-loading";

/** ChatPage's localized copy: the ``chatPageX`` strings, a ref for long-lived PTY effects, and the
 *  active-locale wording for the English connection constants the PTY state stores verbatim. */
export function useChatPageCopy(t: Translations) {
  const s = useMemo(() => localeSection(t, "chatPageX"), [t]);
  // The PTY effects below outlive renders; they read the current copy through this ref so a
  // locale switch never re-runs (and reconnects) the terminal.
  const sRef = useRef(s);
  useEffect(() => {
    sRef.current = s;
  }, [s]);
  // Connection copy lives in lib/pty-*.ts as English constants (state stores them verbatim);
  // the page renders the active locale's wording for each known constant.
  const ptyCopy = useMemo(
    () =>
      new Map<string, string>([
        [PTY_TOKEN_MISSING_BANNER.text, s.tokenMissing],
        [ptyRejectionBanner(4401)?.text ?? "", s.rejectedExpired],
        [ptyRejectionBanner(4403)?.text ?? "", s.rejectedHostMismatch],
        [ptyRejectionBanner(4404)?.text ?? "", s.rejectedNoPty],
        [ptyRejectionBanner(4408)?.text ?? "", s.rejectedNonLoopback],
        [PTY_RECONNECTING_BANNER, s.reconnectingBanner],
        [PTY_GAVE_UP_BANNER.text, s.gaveUp],
        [PTY_SESSION_ENDED_MESSAGE, s.sessionEnded],
        [PTY_START_FAILED_MESSAGE, s.startFailed],
        [PTY_RESUME_LOADING_MESSAGE, s.resumeLoading],
      ]),
    [s],
  );
  const ptyText = (text: string) => ptyCopy.get(text) ?? text;
  return { s, sRef, ptyText };
}
