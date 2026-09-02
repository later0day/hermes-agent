import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router";
import { Search, X, MessageSquare } from "lucide-react";
import { api } from "@/lib/api";
import { useI18n } from "@/i18n";
import type { SessionInfo, SessionSearchResult } from "@/lib/api";

/**
 * Global Cmd/Ctrl+K session search palette.
 *
 * A lightweight command-palette overlay mounted once at the app shell so any
 * page can jump to a past conversation without navigating to the Sessions
 * page first. Empty query shows the 10 most-recent sessions; typing runs a
 * debounced full-text search via `/api/sessions/search`. Arrow keys move the
 * cursor, Enter opens the highlighted session in Chat, Escape closes.
 */

interface UnifiedSession {
  id: string;
  title?: string | null;
  source?: string | null;
  last_active?: number | null;
}

function toUnified(s: SessionInfo): UnifiedSession {
  return { id: s.id, title: s.title, source: s.source, last_active: s.last_active };
}

function toUnifiedFromSearch(s: SessionSearchResult): UnifiedSession {
  return {
    id: s.session_id,
    title: s.snippet || s.session_id,
    source: s.source,
    last_active: s.session_started,
  };
}

interface Props {
  open: boolean;
  onClose: () => void;
}

export function SessionSearchModal({ open, onClose }: Props) {
  const { t } = useI18n();
  const navigate = useNavigate();
  const inputRef = useRef<HTMLInputElement>(null);
  const [query, setQuery] = useState("");
  const [sessions, setSessions] = useState<UnifiedSession[]>([]);
  const [loading, setLoading] = useState(false);
  const [cursor, setCursor] = useState(0);

  // Focus input + reset state whenever the palette opens.
  useEffect(() => {
    if (open) {
      setTimeout(() => inputRef.current?.focus(), 50);
      setQuery("");
      setCursor(0);
    }
  }, [open]);

  // Fetch on open + on (debounced) query change. Empty query → recent list.
  useEffect(() => {
    if (!open) return;
    setLoading(true);
    const timer = setTimeout(async () => {
      try {
        if (query) {
          const res = await api.searchSessions(query);
          setSessions((res.results ?? []).map(toUnifiedFromSearch));
        } else {
          const res = await api.getSessions(10, 0, undefined, "recent");
          setSessions((res.sessions ?? []).map(toUnified));
        }
      } catch {
        setSessions([]);
      } finally {
        setLoading(false);
      }
    }, query ? 200 : 0);
    return () => clearTimeout(timer);
  }, [query, open]);

  const select = useCallback(
    (s: UnifiedSession) => {
      navigate(`/chat?session=${s.id}`);
      onClose();
    },
    [navigate, onClose],
  );

  const handleKey = (e: React.KeyboardEvent) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setCursor((c) => Math.min(c + 1, sessions.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setCursor((c) => Math.max(c - 1, 0));
    } else if (e.key === "Enter" && sessions[cursor]) {
      select(sessions[cursor]);
    } else if (e.key === "Escape") {
      onClose();
    }
  };

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center pt-[15vh] bg-black/60 backdrop-blur-sm"
      onClick={onClose}
    >
      <div
        className="w-full max-w-xl mx-4 rounded-xl border border-border bg-background shadow-2xl overflow-hidden"
        onClick={(e) => e.stopPropagation()}
        onKeyDown={handleKey}
      >
        <div className="flex items-center gap-3 px-4 py-3 border-b border-border">
          <Search className="h-4 w-4 text-muted-foreground shrink-0" />
          <input
            ref={inputRef}
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              setCursor(0);
            }}
            placeholder={t.sessions.searchGlobalPlaceholder ?? "Search sessions…"}
            className="flex-1 bg-transparent text-sm outline-none placeholder:text-muted-foreground"
          />
          {loading && <span className="text-xs text-muted-foreground">…</span>}
          <button onClick={onClose} className="text-muted-foreground hover:text-foreground">
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="max-h-[50vh] overflow-y-auto">
          {sessions.length === 0 && !loading && (
            <div className="px-4 py-8 text-center text-sm text-muted-foreground">
              {query
                ? t.sessions.noMatch
                : t.sessions.searchNoRecent ?? "No recent sessions"}
            </div>
          )}
          {sessions.map((s, i) => (
            <button
              key={s.id}
              className={`w-full flex items-center gap-3 px-4 py-3 text-left hover:bg-muted/50 transition-colors ${
                i === cursor ? "bg-muted/50" : ""
              }`}
              onClick={() => select(s)}
              onMouseEnter={() => setCursor(i)}
            >
              <MessageSquare className="h-4 w-4 text-muted-foreground shrink-0" />
              <div className="flex-1 min-w-0">
                <div className="text-sm font-medium truncate">{s.title || s.id}</div>
                {s.source && (
                  <div className="text-xs text-muted-foreground">{s.source}</div>
                )}
              </div>
              {s.last_active != null && (
                <span className="text-xs text-muted-foreground shrink-0">
                  {new Date(s.last_active * 1000).toLocaleDateString()}
                </span>
              )}
            </button>
          ))}
        </div>

        <div className="flex gap-3 px-4 py-2 border-t border-border text-xs text-muted-foreground">
          <span>{t.sessions.searchHintNavigate ?? "↑↓ navigate"}</span>
          <span>{t.sessions.searchHintOpen ?? "↵ open"}</span>
          <span>{t.sessions.searchHintClose ?? "esc close"}</span>
        </div>
      </div>
    </div>
  );
}
