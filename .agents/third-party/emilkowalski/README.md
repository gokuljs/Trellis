# Emil Kowalski skills for Trellis

The skills recorded with source `emilkowalski/skill` in
[`skills-lock.json`](../../../skills-lock.json) come from
[emilkowalski/skills](https://github.com/emilkowalski/skills), commit
`a81dbca07f2217b7103e510c0f13f8d5b017afc9`. GitHub redirects the original
repository name to this source.

Trellis uses 14 web and UI skills from this source. The React Native/Expo
`animate-expo` and Swift `write-swift` skills are excluded because Trellis's
frontend uses Vite and React on the web. Mobile-web guidance remains relevant.
See pstack's [frontend craft reference](../../skills/pstack/references/frontend-craft.md)
for how these skills apply to the existing components, motion, and Sonner toasts.

The skill files under [`.agents/skills`](../../skills/) retain their upstream
contents. Their MIT copyright and permission notice is preserved in
[LICENSE](LICENSE).

Install this selection for the project's Codex sessions with:

```sh
npx skills add emilkowalski/skill --agent codex --yes \
  --skill emil-design-eng animate animation-performance animation-vocabulary \
  apple-design ask-sonner break-ui find-animation-opportunities \
  improve-animations mobile-native pick-ui-library prototype \
  remove-animations review-animations
```

Use these skills within Trellis's pstack workflow. The root and scoped
`AGENTS.md` files define repository conventions and verification requirements.
`prototype`, `pick-ui-library`, and `review-animations` require explicit
invocation.
