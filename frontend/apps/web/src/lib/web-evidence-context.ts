import { createContext, useContext } from "react"

import type { SourceSelection } from "@/lib/web-evidence"

type EvidenceContext = {
  ownerId: string
  sessionId: string
  revision: number
  openSource: (selection: SourceSelection) => void
}

export const WebEvidenceContext = createContext<EvidenceContext | null>(null)
export const useWebEvidence = () => useContext(WebEvidenceContext)
