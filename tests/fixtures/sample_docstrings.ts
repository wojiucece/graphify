/** Dispatch a settlement batch for the night run. */
export function dispatchBatch(batchId: string): void {}

/** Renders a widget into the host DOM. */
export class WidgetRenderer {
  /** Build the renderer with a mount point. */
  constructor(mount: HTMLElement) {}

  /** Paint the widget at the current size. */
  render(): void {}

  /** Clear the mount point for the next frame. */
  private clear(): void {}
}

/** Prepare the canvas for a new frame. */
export const prepareFrame = (): void => {};

/* A plain block comment, not JSDoc, must not attach. */
function plainBlock(): void {}

// A line comment, not JSDoc, must not attach.
function lineComment(): void {}
