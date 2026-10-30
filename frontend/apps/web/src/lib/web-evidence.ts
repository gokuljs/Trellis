export type WebSource = {
  id: string
  session_id: string
  run_id: string
  tool_call_id: string
  requested_url: string | null
  final_url: string | null
  canonical_url: string | null
  title: string | null
  publisher: string | null
  published_at: string | null
  fetched_at: string | null
  extractor_version: string | null
  content_hash: string | null
  line_count: number
  byte_count: number
  status: string
  error_code: string | null
  created_at: string
}

export type WebSourcePage = { items: WebSource[]; next_offset: number | null }
export type WebSourceLines = {
  source: WebSource
  start_line: number
  lines: string[]
  total_lines: number
}
export type CitationReference = {
  source_id: string
  start_line: number
  end_line: number
}
export type CitationBinding = CitationReference & {
  model_call_id: string
  citation_index: number
}
export type CitationToken = {
  raw: string
  reference: CitationReference | null
}
export type SourceSelection = CitationReference

const uuid =
  "[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
const marker = new RegExp(
  `^\\[\\[source:(${uuid}):L([1-9][0-9]{0,5})(?:-L([1-9][0-9]{0,5}))?\\]\\]$`
)

export function citationKey(reference: CitationReference): string {
  return `${reference.source_id.toLowerCase()}:${reference.start_line}:${reference.end_line}`
}

export function citationTokens(text: string): (string | CitationToken)[] {
  const parts: (string | CitationToken)[] = []
  let cursor = 0
  for (const match of text.matchAll(/\[\[source:[^\n]*?(?:\]\]|$)/g)) {
    if (match.index > cursor) parts.push(text.slice(cursor, match.index))
    const parsed = marker.exec(match[0])
    const start = Number(parsed?.[2])
    const end = Number(parsed?.[3] ?? parsed?.[2])
    parts.push({
      raw: match[0],
      reference:
        parsed &&
        Number.isSafeInteger(start) &&
        Number.isSafeInteger(end) &&
        start >= 1 &&
        end >= start &&
        end <= 262_145 &&
        end - start < 200
          ? {
              source_id: parsed[1]!.toLowerCase(),
              start_line: start,
              end_line: end,
            }
          : null,
    })
    cursor = match.index + match[0].length
  }
  if (cursor < text.length) parts.push(text.slice(cursor))
  return parts
}

type MarkdownNode = {
  type: string
  value?: string
  children?: MarkdownNode[]
  data?: { hName?: string; hProperties?: Record<string, string> }
  position?: { start: { offset?: number }; end: { offset?: number } }
}

export function transformCitationTree(
  tree: MarkdownNode,
  source: string
): CitationReference[] {
  const references = new Map<string, CitationReference>()
  const visit = (node: MarkdownNode) => {
    if (
      [
        "code",
        "inlineCode",
        "link",
        "linkReference",
        "image",
        "imageReference",
        "definition",
      ].includes(node.type)
    )
      return
    if (!node.children) return
    node.children = node.children.flatMap((child) => {
      if (child.type !== "text" || !child.value) {
        visit(child)
        return [child]
      }
      const start = child.position?.start.offset
      const end = child.position?.end.offset
      if (
        start === undefined ||
        end === undefined ||
        source.slice(start, end) !== child.value
      )
        return [child]
      return citationTokens(child.value).map((part): MarkdownNode => {
        if (typeof part === "string") return { type: "text", value: part }
        if (part.reference)
          references.set(citationKey(part.reference), part.reference)
        return {
          type: "trellisCitation",
          data: {
            hName: "span",
            hProperties: {
              "data-citation": part.reference
                ? citationKey(part.reference)
                : "invalid",
            },
          },
          children: [{ type: "text", value: part.raw }],
        }
      })
    })
  }
  visit(tree)
  return [...references.values()]
}

export function remarkCitations({ source }: { source: string }) {
  return (tree: MarkdownNode) => {
    transformCitationTree(tree, source)
  }
}

export function safePublicSourceUrl(
  value: string | null | undefined
): string | null {
  if (!value) return null
  try {
    if (/redacted/i.test(decodeURIComponent(value))) return null
    const url = new URL(value)
    const host = url.hostname.toLowerCase().replace(/\.$/, "")
    if (
      !["http:", "https:"].includes(url.protocol) ||
      url.username ||
      url.password ||
      !host.includes(".") ||
      host.includes(":") ||
      /^[0-9.]+$/.test(host) ||
      /(?:^|\.)(?:localhost|local|internal|home|lan)$/.test(host)
    )
      return null
    return url.href
  } catch {
    return null
  }
}

export function sourceStatusLabel(source: WebSource): string {
  if (source.status === "cleared") return "Source cleared"
  if (source.status === "success") return "Recorded source"
  if (source.status === "partial") return "Partial source"
  return source.status.replaceAll("_", " ")
}
