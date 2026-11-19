import { useQuery } from "@tanstack/react-query";

import { apiGet } from "@/api/client";
import type { Profile } from "@/types/api";

/** Every resume, active ones first in tie-break order; polls while one parses. */
export function useProfiles() {
  return useQuery({
    queryKey: ["profile", "list"],
    queryFn: () => apiGet<Profile[]>("/api/v1/profile"),
    refetchInterval: (query) =>
      query.state.data?.some((profile) => profile.parse_status === "pending")
        ? 3_000
        : false,
  });
}
