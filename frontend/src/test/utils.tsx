/** Test harness: the providers every view needs, plus a fetch stub that records what the app
 *  asked for. Nothing here reaches a real backend, a real container or a real cgroup. */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, type RenderResult } from "@testing-library/react";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { vi } from "vitest";

import { TooltipProvider } from "@/components/ui/tooltip";

export function renderApp(ui: ReactElement): RenderResult {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <TooltipProvider>
        <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>{ui}</MemoryRouter>
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

export interface FetchCall { url: string; method: string; body: unknown }

/** Installs a fetch stub serving `routes`, and records every call for assertions. */
export function stubFetch(routes: Record<string, unknown | (() => unknown)>) {
  const calls: FetchCall[] = [];
  const handler = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = (init?.method ?? "GET").toUpperCase();
    calls.push({ url, method, body: init?.body ? JSON.parse(String(init.body)) : undefined });
    const key = Object.keys(routes).find((k) => url.startsWith(k));
    if (key === undefined) {
      return new Response(JSON.stringify({ detail: { message: `no stub for ${url}` } }), { status: 404 });
    }
    const value = routes[key];
    const payload = typeof value === "function" ? (value as () => unknown)() : value;
    return new Response(JSON.stringify(payload), {
      status: 200, headers: { "Content-Type": "application/json" },
    });
  });
  vi.stubGlobal("fetch", handler);
  return { calls, handler };
}

/** Minimal EventSource stand-in: jsdom has none, and the real one would open a socket. */
export class FakeEventSource {
  static instances: FakeEventSource[] = [];
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;
  private listeners = new Map<string, ((e: MessageEvent<string>) => void)[]>();

  constructor(readonly url: string) {
    FakeEventSource.instances.push(this);
  }

  addEventListener(type: string, fn: (e: MessageEvent<string>) => void) {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), fn]);
  }

  close() { this.closed = true; }

  /** Delivers one `stage` event, exactly as the backend's SSE generator would. */
  push(data: unknown, type = "stage") {
    const ev = new MessageEvent<string>(type, { data: JSON.stringify(data) });
    for (const fn of this.listeners.get(type) ?? []) fn(ev);
  }

  static reset() { FakeEventSource.instances = []; }
  static get last() { return FakeEventSource.instances[FakeEventSource.instances.length - 1]; }
}
