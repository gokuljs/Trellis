import { useEffect, useState, type ReactNode } from "react"
import { QueryClientProvider } from "@tanstack/react-query"

import {
  createSessionQueryClient,
  disposeSessionQueryClient,
} from "@/lib/query-client"

export function SessionQueryProvider({ children }: { children: ReactNode }) {
  const [client] = useState(createSessionQueryClient)
  useEffect(() => () => disposeSessionQueryClient(client), [client])
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>
}
