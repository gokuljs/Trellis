# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Assumption based on the README: developers and researchers using a local
Trellis installation for coding and research. The primary audience and its
specific working context have not been confirmed.

## Product Purpose

The README describes Trellis as a self-improving multi-agent system for coding
and research. It coordinates specialized agents so they can work together,
learn from previous tasks, and improve how they solve problems.

## Positioning

Open decision. The README does not establish a specific differentiator beyond
the product description above.

## Operating Context

The documented setup runs the frontend and backend locally. Users configure an
OpenAI or Anthropic API key in Settings. Saved sessions restore from `~/.trellis`
by default; `TRELLIS_DATA_DIR` can point storage elsewhere.

## Capabilities and Constraints

- The current interface provides local chat, model settings, workspaces, and
  saved sessions.
- The README documents OpenAI and Anthropic provider setup.
- Audience-specific requirements and any broader deployment model remain open.

## Brand Commitments

The product name is Trellis. No additional voice or brand commitments have been
confirmed.

## Evidence on Hand

The repository contains the current frontend and backend implementation and a
product screenshot in `README.md`. Do not invent testimonials, benchmarks, or
other product claims.

## Product Principles

These are provisional readings of the repository description, not separately
confirmed strategy:

- Coordinate specialized agents for coding and research.
- Preserve useful continuity across tasks.
- Improve agent work through experience from previous tasks.
