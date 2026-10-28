# Pinned pstack source

This directory contains Lauren Tan's official [pstack](https://github.com/cursor/plugins/tree/main/pstack)
from `cursor/plugins`, MIT licensed. [upstream.json](upstream.json) records the
full commit, Git tree, version, import date, and file count.

`upstream/` is a complete, unchanged copy of that commit's `pstack/` directory,
including its license, skills, agent prompts, playbooks, scripts, and docs. It
is reference material outside native skill discovery. Trellis's engineering
workflow entrypoint is [pstack](../skills/pstack/SKILL.md). The
[grill-me](../skills/grill-me/SKILL.md) and
[grilling](../skills/grilling/SKILL.md) skills provide complementary design
interviews.

The original manifests, global rule paths, model names, tools, and automations
describe Cursor. Their presence does not install or activate Cursor features.
No upstream scripts or packages are needed for pstack's Markdown workflows.
Review compatibility and effects before using an advanced script or helper.

## Check the snapshot

From the repository root:

```bash
(cd .agents/pstack && shasum -a 256 -c upstream.sha256)
```

All files must report `OK`. Checksums verify file contents; the source commit
and Git tree identify the upstream revision. The checksum list stays outside
the immutable source tree.

## Update deliberately

1. Use a separate clone of `https://github.com/cursor/plugins.git` and check out
   the full commit you intend to import. Include `pstack/` when using a sparse
   clone.
2. Review upstream changes, including licenses, helper scripts, model and tool
   assumptions, and relative links.
3. Replace `upstream/` with the complete new `pstack/` tree. Remove files deleted
   upstream, preserve executable bits, and include hidden files.
4. Update `upstream.json` with the full commit (`git rev-parse HEAD`), tree
   (`git rev-parse HEAD:pstack`), version from `pstack/.cursor-plugin/plugin.json`,
   import date, and tracked file count.
5. Regenerate `upstream.sha256` from the imported tracked files, then compare
   paths, bytes, and executable bits with the source checkout.
6. Recheck the pstack adapter and repository guidance against changed skills,
   playbooks, prompts, and companion-tool assumptions. Verify hashes and have
   an independent reviewer inspect the update. Commit source and adapter changes
   as separate commits only when each commit remains usable.

Keep engineering workflow guidance in the pstack entrypoint. Add future
Trellis engineering guidance as a focused reference under
`.agents/skills/pstack/references/` and route to it through the pstack adapter.
