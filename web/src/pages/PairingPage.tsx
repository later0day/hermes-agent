import { useCallback, useEffect, useLayoutEffect, useState } from "react";
import { Check, ShieldCheck, Trash2, Users, X } from "lucide-react";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { H2 } from "@nous-research/ui/ui/components/typography/h2";
import { api } from "@/lib/api";
import type { PairingResponse, PairingUser } from "@/lib/api";
import { DeleteConfirmDialog } from "@/components/DeleteConfirmDialog";
import { useToast } from "@nous-research/ui/hooks/use-toast";
import { useConfirmDelete } from "@nous-research/ui/hooks/use-confirm-delete";
import { Toast } from "@nous-research/ui/ui/components/toast";
import { Card, CardContent } from "@nous-research/ui/ui/components/card";
import { Input } from "@nous-research/ui/ui/components/input";
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
  const [pending, setPending] = useState<PairingUser[]>([]);
  const [approved, setApproved] = useState<PairingUser[]>([]);
  const [loading, setLoading] = useState(true);
  const [approving, setApproving] = useState<string | null>(null);
  const [clearing, setClearing] = useState(false);
  // Manual one-time codes typed by the operator, keyed by pending-user key.
  // Some platforms surface a code the user relays in-DM rather than a
  // request_id the admin can click; the backend accepts either through the
  // same endpoint (it disambiguates by shape), so a filled code takes
  // precedence over the request_id when approving.
  const [manualCodes, setManualCodes] = useState<Record<string, string>>({});
  const { toast, showToast } = useToast();
  const { setEnd } = usePageHeader();

  const loadPairing = useCallback(() => {
    api
      .getPairing()
      .then((res: PairingResponse) => {
        setPending(res.pending);
        setApproved(res.approved);
      })
      .catch(() => showToast("Failed to load pairing requests", "error"))
      .finally(() => setLoading(false));
  }, [showToast]);

  useEffect(() => {
    loadPairing();
  }, [loadPairing]);

  const handleApprove = async (user: PairingUser) => {
    const key = getUserKey(user);
    const code = (manualCodes[key] || "").trim().toUpperCase();
    // A typed code wins; otherwise fall back to the click-to-approve
    // request_id. One of the two must be present.
    const approveArg = code || user.request_id;
    if (!approveArg) {
      showToast("Enter a pairing code or missing request", "error");
      return;
    }
    setApproving(key);
    try {
      await api.approvePairing(user.platform, approveArg);
      showToast(`Approved: "${getUserLabel(user)}"`, "success");
      setManualCodes((prev) => {
        const next = { ...prev };
        delete next[key];
        return next;
      });
      loadPairing();
    } catch (e) {
      showToast(`Error: ${e}`, "error");
    } finally {
      setApproving(null);
    }
  };

  const handleClearPending = async () => {
    if (!window.confirm("Clear all pending pairing requests?")) return;
    setClearing(true);
    try {
      const res = await api.clearPendingPairing();
      showToast(`Cleared ${res.cleared} pending request(s)`, "success");
      loadPairing();
    } catch (e) {
      showToast(`Error: ${e}`, "error");
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
            `Revoked: "${user ? getUserLabel(user) : user_id}"`,
            "success",
          );
          loadPairing();
        } catch (e) {
          showToast(`Error: ${e}`, "error");
          throw e;
        }
      },
      [approved, loadPairing, showToast],
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
      >
        Clear pending
      </Button>,
    );
    return () => {
      setEnd(null);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [setEnd, clearing]);

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
        title="Revoke access"
        description={
          pendingRevokeUser
            ? `"${getUserLabel(pendingRevokeUser)}" will lose access. This cannot be undone.`
            : "This user will lose access. This cannot be undone."
        }
        confirmLabel="Revoke"
        loading={userRevoke.isDeleting}
      />

      {/* Pending requests */}
      <div className="flex flex-col gap-3">
        <H2
          variant="sm"
          className="flex items-center gap-2 text-muted-foreground"
        >
          <Users className="h-4 w-4" />
          Pending requests ({pending.length})
        </H2>

        {pending.length === 0 && (
          <Card>
            <CardContent className="py-8 text-center text-sm text-muted-foreground">
              No pending pairing requests
            </CardContent>
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
                    <span className="font-medium text-sm truncate">
                      {getUserLabel(user)}
                    </span>
                  </div>
                  <div className="flex items-center gap-4 text-xs text-muted-foreground">
                    <span className="truncate">{user.user_id}</span>
                    {typeof user.age_minutes === "number" && (
                      <span>{user.age_minutes}m ago</span>
                    )}
                  </div>
                </div>

                <div className="flex items-center gap-1 shrink-0">
                  <Input
                    value={manualCodes[key] || ""}
                    onChange={(e) =>
                      setManualCodes((prev) => ({
                        ...prev,
                        [key]: e.target.value,
                      }))
                    }
                    onKeyDown={(e) => {
                      if (e.key === "Enter") handleApprove(user);
                    }}
                    placeholder="Code (optional)"
                    className="h-8 w-32 text-xs uppercase"
                  />
                  <Button
                    size="sm"
                    className="uppercase"
                    onClick={() => handleApprove(user)}
                    disabled={
                      approving === key ||
                      (!user.request_id && !(manualCodes[key] || "").trim())
                    }
                    prefix={
                      approving === key ? (
                        <Spinner />
                      ) : (
                        <Check className="h-4 w-4" />
                      )
                    }
                  >
                    Approve
                  </Button>
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
          Approved users ({approved.length})
        </H2>

        {approved.length === 0 && (
          <Card>
            <CardContent className="py-8 text-center text-sm text-muted-foreground">
              No approved users
            </CardContent>
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
                    title="Revoke"
                    aria-label="Revoke"
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
