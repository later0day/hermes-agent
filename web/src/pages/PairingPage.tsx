import { useI18n } from "@/i18n";
import { en } from "@/i18n/en";
import { useCallback, useEffect, useLayoutEffect, useState } from "react";
import { Check, ShieldCheck, Trash2, Users, X } from "lucide-react";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import { Input } from "@nous-research/ui/ui/components/input";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { H2 } from "@nous-research/ui/ui/components/typography/h2";
import { api } from "@/lib/api";
import type { PairingResponse, PairingUser } from "@/lib/api";
import { DeleteConfirmDialog } from "@/components/DeleteConfirmDialog";
import { useToast } from "@nous-research/ui/hooks/use-toast";
import { useConfirmDelete } from "@nous-research/ui/hooks/use-confirm-delete";
import { Toast } from "@nous-research/ui/ui/components/toast";
import { Card, CardContent } from "@nous-research/ui/ui/components/card";
import { usePageHeader } from "@/contexts/usePageHeader";

function getUserKey(user: PairingUser): string {
  return `${user.platform}:${user.user_id}`;
}

function splitUserKey(key: string): { platform: string; user_id: string } {
  const idx = key.indexOf(":");
  if (idx === -1) return { platform: "", user_id: key };
  return { platform: key.slice(0, idx), user_id: key.slice(idx + 1) };
}

function getUserLabel(user: PairingUser): string {
  return user.user_name || user.user_id;
}

export default function PairingPage() {
  const { t } = useI18n();
  const tPairing = (t.pairing ?? en.pairing) as Record<string, string>;

  const [pending, setPending] = useState<PairingUser[]>([]);
  const [approved, setApproved] = useState<PairingUser[]>([]);
  const [loading, setLoading] = useState(true);
  const [approving, setApproving] = useState<string | null>(null);
  const [manualCodes, setManualCodes] = useState<Record<string, string>>({});
  const [clearing, setClearing] = useState(false);
  const { toast, showToast } = useToast();
  const { setEnd } = usePageHeader();

  const loadPairing = useCallback(() => {
    api
      .getPairing()
      .then((res: PairingResponse) => {
        setPending(res.pending);
        setApproved(res.approved);
      })
      .catch(() => showToast(tPairing.loadFailed, "error"))
      .finally(() => setLoading(false));
  }, [showToast, t]);

  useEffect(() => {
    loadPairing();
  }, [loadPairing]);

  const handleApprove = async (user: PairingUser) => {
    const key = getUserKey(user);
    const code = (manualCodes[key] || "").trim().toUpperCase();
    if (!code) {
      showToast(tPairing.missingCode, "error");
      return;
    }
    setApproving(key);
    try {
      await api.approvePairing(user.platform, code);
      showToast(tPairing.approved.replace("{name}", getUserLabel(user)), "success");
      setManualCodes((prev) => {
        const next = { ...prev };
        delete next[key];
        return next;
      });
      loadPairing();
    } catch (e) {
      showToast(`${tPairing.error}: ${e}`, "error");
    } finally {
      setApproving(null);
    }
  };

  const handleClearPending = async () => {
    if (!window.confirm(tPairing.clearConfirm)) return;
    setClearing(true);
    try {
      const res = await api.clearPendingPairing();
      showToast(tPairing.clearedCount.replace("{count}", String(res.cleared)), "success");
      loadPairing();
    } catch (e) {
      showToast(`${tPairing.error}: ${e}`, "error");
    } finally {
      setClearing(false);
    }
  };

  const userRevoke = useConfirmDelete({
    onDelete: useCallback(
      async (key: string) => {
        const { platform, user_id } = splitUserKey(key);
        const user = approved.find((u) => getUserKey(u) === key);
        try {
          await api.revokePairing(platform, user_id);
          showToast(
            tPairing.revoked.replace("{name}", user ? getUserLabel(user) : user_id),
            "success",
          );
          loadPairing();
        } catch (e) {
          showToast(`${tPairing.error}: ${e}`, "error");
          throw e;
        }
      },
      [approved, loadPairing, showToast, t],
    ),
  });

  // Put "Clear pending" button in page header
  useLayoutEffect(() => {
    setEnd(
      <Button
        className="uppercase"
        size="sm"
        onClick={handleClearPending}
        disabled={clearing}
        prefix={clearing ? <Spinner /> : <Trash2 className="h-4 w-4" />}
      >{tPairing.clearPending}</Button>,
    );
    return () => {
      setEnd(null);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [setEnd, clearing, t]);

  if (loading) {
    return (
      <div className="flex items-center justify-center py-24">
        <Spinner className="text-2xl text-primary" />
      </div>
    );
  }

  const pendingRevokeUser = userRevoke.pendingId
    ? approved.find((u) => getUserKey(u) === userRevoke.pendingId)
    : null;

  return (
    <div className="flex flex-col gap-6">
      <Toast toast={toast} />

      <DeleteConfirmDialog
        open={userRevoke.isOpen}
        onCancel={userRevoke.cancel}
        onConfirm={userRevoke.confirm}
        title={tPairing.revokeAccess}
        description={
          pendingRevokeUser
            ? tPairing.revokeDescUser.replace("{name}", getUserLabel(pendingRevokeUser))
            : tPairing.revokeDescGeneric
        }
        confirmLabel={tPairing.revokeLabel}
        loading={userRevoke.isDeleting}
      />

      {/* Pending requests */}
      <div className="flex flex-col gap-3">
        <H2
          variant="sm"
          className="flex items-center gap-2 text-muted-foreground"
        >
          <Users className="h-4 w-4" />
          {tPairing.pendingRequests} ({pending.length})
        </H2>

        {pending.length === 0 && (
          <Card>
            <CardContent className="py-8 text-center text-sm text-muted-foreground">{tPairing.noPending}</CardContent>
          </Card>
        )}

        {pending.map((user) => {
          const key = getUserKey(user);
          return (
            <Card key={key}>
              <CardContent className="flex items-start gap-4 py-4">
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 mb-1">
                    <Badge tone="outline">{user.platform}</Badge>
                    {user.code_hint && (
                      <span
                        className="font-mono text-sm text-muted-foreground"
                        title="Stored hash prefix, not the pairing code"
                      >
                        hash:{user.code_hint}
                      </span>
                    )}
                  </div>
                  <div className="flex items-center gap-4 text-xs text-muted-foreground">
                    <span className="truncate">{user.user_id}</span>
                    {user.user_name && (
                      <span className="truncate">{user.user_name}</span>
                    )}
                    {typeof user.age_minutes === "number" && (
                      <span>{tPairing.mAgo.replace("{count}", String(user.age_minutes))}</span>
                    )}
                  </div>
                </div>

                <div className="flex w-full shrink-0 flex-col gap-2 sm:w-auto sm:min-w-72 sm:flex-row sm:items-center">
                  <Input
                    className="h-8 font-mono text-xs uppercase"
                    placeholder={tPairing.missingCode}
                    value={manualCodes[key] || ""}
                    onChange={(e) =>
                      setManualCodes((prev) => ({
                        ...prev,
                        [key]: e.target.value.toUpperCase(),
                      }))
                    }
                    onKeyDown={(e) => {
                      if (e.key === "Enter") {
                        void handleApprove(user);
                      }
                    }}
                    autoCapitalize="characters"
                    autoComplete="one-time-code"
                  />
                  <Button
                    size="sm"
                    className="uppercase"
                    onClick={() => handleApprove(user)}
                    disabled={approving === key || !(manualCodes[key] || "").trim()}
                    prefix={
                      approving === key ? (
                        <Spinner />
                      ) : (
                        <Check className="h-4 w-4" />
                      )
                    }
                  >{tPairing.approveLabel}</Button>
                </div>
              </CardContent>
            </Card>
          );
        })}
      </div>

      {/* Approved users */}
      <div className="flex flex-col gap-3">
        <H2
          variant="sm"
          className="flex items-center gap-2 text-muted-foreground"
        >
          <ShieldCheck className="h-4 w-4" />
          {tPairing.approvedUsers} ({approved.length})
        </H2>

        {approved.length === 0 && (
          <Card>
            <CardContent className="py-8 text-center text-sm text-muted-foreground">{tPairing.noApproved}</CardContent>
          </Card>
        )}

        {approved.map((user) => {
          const key = getUserKey(user);
          return (
            <Card key={key}>
              <CardContent className="flex items-start gap-4 py-4">
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 mb-1">
                    <Badge tone="outline">{user.platform}</Badge>
                    <span className="font-medium text-sm truncate">
                      {user.user_id}
                    </span>
                  </div>
                  {user.user_name && (
                    <div className="text-xs text-muted-foreground truncate">
                      {user.user_name}
                    </div>
                  )}
                </div>

                <div className="flex items-center gap-1 shrink-0">
                  <Button
                    ghost
                    size="icon"
                    title={tPairing.revokeLabel}
                    aria-label={tPairing.revokeLabel}
                    className="text-destructive"
                    onClick={() => userRevoke.requestDelete(key)}
                  >
                    <X />
                  </Button>
                </div>
              </CardContent>
            </Card>
          );
        })}
      </div>
    </div>
  );
}
