import "@testing-library/jest-dom/vitest";

/** jsdom implements neither of these, and Radix primitives (Slider, Select, Tooltip) and the
 *  ECharts wrapper call them during layout. Stubbing them here keeps the component tests about
 *  behaviour rather than about jsdom's gaps. */

if (!globalThis.ResizeObserver) {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver;
}

if (!globalThis.matchMedia) {
  globalThis.matchMedia = ((query: string) => ({
    matches: false, media: query, onchange: null,
    addListener() {}, removeListener() {},
    addEventListener() {}, removeEventListener() {}, dispatchEvent: () => false,
  })) as unknown as typeof matchMedia;
}

// Radix uses pointer capture APIs that jsdom's HTMLElement does not define.
for (const name of ["hasPointerCapture", "setPointerCapture", "releasePointerCapture"] as const) {
  if (!(name in Element.prototype)) {
    Object.defineProperty(Element.prototype, name, { value: () => false, writable: true });
  }
}

if (!Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = () => {};
}
