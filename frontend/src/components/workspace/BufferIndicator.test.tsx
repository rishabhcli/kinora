import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { BufferIndicator } from "./BufferIndicator";

afterEach(cleanup);

describe("BufferIndicator", () => {
  it("fills toward H and reports an ok/healthy buffer", () => {
    render(<BufferIndicator committedSecondsAhead={37.5} low={25} high={75} />);
    const fill = screen.getByTestId("buffer-fill");
    expect(fill.style.width).toBe("50%");
    expect(fill.getAttribute("data-health")).toBe("ok");

    const meter = screen.getByRole("meter");
    expect(meter.getAttribute("aria-valuenow")).toBe("38");
    expect(meter.getAttribute("aria-valuemax")).toBe("75");
  });

  it("clamps the fill at the high watermark", () => {
    render(<BufferIndicator committedSecondsAhead={120} low={25} high={75} />);
    expect(screen.getByTestId("buffer-fill").style.width).toBe("100%");
    expect(screen.getByTestId("buffer-fill").getAttribute("data-health")).toBe("full");
  });

  it("flags a low buffer below the low watermark", () => {
    render(<BufferIndicator committedSecondsAhead={10} low={25} high={75} />);
    expect(screen.getByTestId("buffer-fill").getAttribute("data-health")).toBe("low");
  });
});
