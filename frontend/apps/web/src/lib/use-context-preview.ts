import { useEffect, useState } from "react"
import { useQuery } from "@tanstack/react-query"

import type { ContextPreviewRequest } from "@/lib/app-types"
import { delayContextPreview, previewDraftTooLong } from "@/lib/context-gauge"
import { contextPreviewQueryOptions } from "@/lib/server-queries"

export function useContextPreview(
  input: ContextPreviewRequest,
  generation: number,
  enabled: boolean
) {
  const options = contextPreviewQueryOptions(input, generation)
  const identity = JSON.stringify(options.queryKey)
  const [settledIdentity, setSettledIdentity] = useState<string | null>(null)
  const tooLong = previewDraftTooLong(input.draft)
  useEffect(() => {
    if (!enabled || tooLong) return
    return delayContextPreview(identity, setSettledIdentity)
  }, [enabled, identity, tooLong])
  const settled = enabled && !tooLong && identity === settledIdentity
  const query = useQuery({ ...options, enabled: settled })
  return {
    preview:
      settled && !query.isFetching && !query.isError
        ? (query.data ?? null)
        : null,
    loading: enabled && !tooLong && (!settled || query.isFetching),
    error: tooLong
      ? "Context preview supports drafts up to 32,000 characters. Shorten the draft to see its estimate."
      : settled && query.isError
        ? "Context usage is unavailable. It will update after your next edit."
        : null,
  }
}
