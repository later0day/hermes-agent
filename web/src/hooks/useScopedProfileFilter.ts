import { useState } from "react";
import { useProfileScope } from "@/contexts/useProfileScope";

/** A page-local profile filter that follows the dashboard's global profile switcher: it starts at the
 *  scoped profile ("all" when none is scoped) and re-follows every switch, while the page can still
 *  widen it locally in between. */
export function useScopedProfileFilter(): [string, (profile: string) => void] {
  const { profile: scopedProfile } = useProfileScope();
  const [selected, setSelected] = useState(scopedProfile || "all");
  const [followed, setFollowed] = useState(scopedProfile);
  if (followed !== scopedProfile) {
    setFollowed(scopedProfile);
    setSelected(scopedProfile || "all");
  }
  return [selected, setSelected];
}
