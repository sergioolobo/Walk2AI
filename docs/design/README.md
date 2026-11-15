# Walk2AI — Design

Phase 4 is design-first ([DECISIONS.md](../DECISIONS.md), D-53): research, then a design
system and the main screens, then your review, and only then code.

| Stage | State |
|---|---|
| 4a — Research | Done ([RESEARCH.md](RESEARCH.md)) — from knowledge of established products; no paid tool was used |
| 4b — Visual direction | **Three directions drawn for the dashboard; waiting for your choice** |
| 4b — Design system and main screens | Not started — follows the chosen direction |
| Review of the main screens | Not started |
| 4c — Implementation | Not started — nothing is coded before the review |

## Where the designs are

The designs are on a private design canvas (only you can open it):
<https://claude.ai/artifact/5KQAvqje1nqd1RNbDegARm>

Neither Mobbin nor Figma is used. Mobbin's connector refused to work without a paid
plan; the connected Figma account is on a free plan with a view-only seat, whose
connector allowance I could not confirm would be enough. You decided to spend nothing
and to have the designs built directly. They are drawn as real HTML and CSS, which is
also what the frontend will be built from. Once a direction is chosen, its tokens (colours, type, spacing, radii) and
component rules are written down here as `DESIGN_SYSTEM.md`, so the system stays
documented and reusable whatever happens to the canvas.

## The three directions

All three show the same dashboard with the same sample data, so only the design differs.
Each must make one chain obvious: activity done → credits earned → balance → AI consumed
→ allowance left.

| | A · Night Run | B · Clear Ledger | C · Daybreak |
|---|---|---|---|
| Stance | Dark, dense, a performance instrument | Light, calm, a bank statement | Light, warm, a daily coach |
| Navigation | Left sidebar | Top bar, centred column | Left sidebar, pill items |
| How the chain is shown | Five numbered stations in one strip: moved, earned, balance, AI used, left today | A large balance beside a statement that adds the day up line by line | A sentence headline, an earning ring, and one timeline mixing movement and AI |
| Colour | Near-black; lime for earning, violet for AI | White and ink; orange for earning, blue for AI | White; coral for earning, teal for AI |
| Type | Space Grotesk, Inter, JetBrains Mono | Geist, Geist Mono | Bricolage Grotesque, DM Sans |
| Strength | The whole chain is readable in one glance; feels like a product for someone who trains | Most trustworthy for money: every figure is traceable, with running balances | Friendliest; the timeline tells the story of the day |
| Cost | Dark-first needs care for long reading (chat); a light theme must be designed separately | Least distinctive; closest to a conventional finance tool | Least dense; large shapes leave less room for analytics and tables |

All fonts are free (Google Fonts, open licences). Sample figures follow the real rules:
0.1 WC per step, 5 WC per workout minute, a 1,500 WC daily cap, 1,000 WC = €0.10, and AI
charges computed from the verified prices.

## What does not change with the direction

- The backend, its API and every financial calculation. The interface only displays what
  the API returns; it computes no credits, costs or balances of its own.
- Earning and spending are told apart by more than colour: a sign, a label and position.
- Text contrast of at least 4.5:1; controls at least 44 px tall; real buttons and links.
- The approved stack: Next.js, React, TypeScript, Tailwind CSS, shadcn/ui, Recharts.
