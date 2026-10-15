# Pinned pstack source

This directory contains Lauren Tan's official [pstack](https://github.com/cursor/plugins/tree/main/pstack)
from `cursor/plugins`, MIT licensed. [upstream.json](upstream.json) records the
full commit, Git tree, version, import date, and file count.

`upstream/` is a complete, unchanged copy of that commit's `pstack/` directory,
including its license, agent prompts, playbooks, scripts, and documentation.
It is supporting source outside `.agents/skills/`, so its Cursor entrypoints
are not registered as native Codex skills. The discoverable Trellis entrypoint
is [the pstack adapter](../skills/pstack/SKILL.md).

Keep Trellis routing and harness adaptations outside `upstream/`. The original
Cursor plugin manifest, global rule paths, model slugs, tools, and automation
instructions describe the original runtime. Copying them does not install a
Cursor plugin, enable background jobs, or authorize external actions.
No upstream scripts or package dependencies are required to use the adapter's
Markdown workflows. Advanced tools such as the PR watcher and orchestrator
need a separate compatibility review and task-specific setup before use.

## Check the snapshot

From the repository root:

```bash
(cd .agents/pstack && shasum -a 256 -c upstream.sha256)
```

All files must report `OK`. Checksums verify bytes against this import's
manifest; the source commit and Git tree identify the upstream revision.
The checksum list lives beside the snapshot so upstream files stay unchanged.

## Update deliberately

1. Use a separate clone of `https://github.com/cursor/plugins.git` and check out
   the full commit you intend to import. With a sparse clone, include `pstack/`.
2. Review the diff from the recorded commit, including licenses, scripts,
   model assumptions, and changes to playbooks or relative references.
3. Replace `upstream/` with the complete `pstack/` tree. Remove files deleted
   upstream; do not overlay a new tree onto stale files. Preserve executable
   bits and include hidden files.
4. Update `upstream.json` from that checkout. The commit is `git rev-parse HEAD`,
   the subtree is `git rev-parse HEAD:pstack`, and the version is in
   `pstack/.cursor-plugin/plugin.json`. Record the tracked file count and date.
5. Regenerate checksums from the new tracked files, then compare the imported
   file list, bytes, and executable bits with the checkout. From the Trellis
   root, after replacing the tree:

   ```bash
   python3 - <<'PYCODE'
   from hashlib import sha256
   from pathlib import Path

   bundle = Path('.agents/pstack')
   files = sorted(path for path in (bundle / 'upstream').rglob('*') if path.is_file())
   (bundle / 'upstream.sha256').write_text(''.join(
       f'{sha256(path.read_bytes()).hexdigest()}  {path.relative_to(bundle).as_posix()}\n'
       for path in files
   ))
   PYCODE
   ```

6. Recheck the native adapter against the new entrypoint, playbooks, agent
   prompts, and companion-tool assumptions. Run the checks in
   [the workflow guide](../../docs/agent-workflows.md#verification), including
   the read-only routing scenarios. Commit the snapshot update separately
   from local adaptation changes.

Do not use a floating installer to overwrite `.agents/skills/` or replace
existing skills. `skills-lock.json` tracks skills installed by the skills CLI;
this separate upstream manifest tracks this vendored source.
