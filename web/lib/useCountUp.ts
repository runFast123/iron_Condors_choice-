"use client";

import { useEffect, useRef, useState } from "react";

/**
 * A number that eases to each new value instead of jumping, so a P&L that
 * moves on every tick reads as movement rather than flicker. Honours
 * prefers-reduced-motion: then it simply shows the value.
 */
export function useCountUp(target: number | null, ms = 650): number | null {
  const [shown, setShown] = useState<number | null>(target);
  const from = useRef<number | null>(target);

  useEffect(() => {
    if (target == null) {
      setShown(null);
      from.current = null;
      return;
    }
    const reduce = typeof window !== "undefined"
      && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    const start = from.current;
    if (reduce || start == null || start === target) {
      setShown(target);
      from.current = target;
      return;
    }
    let frame = 0;
    const t0 = performance.now();
    const step = (now: number) => {
      const p = Math.min(1, (now - t0) / ms);
      const eased = 1 - Math.pow(1 - p, 3);
      const value = start + (target - start) * eased;
      setShown(value);
      from.current = value;
      if (p < 1) frame = requestAnimationFrame(step);
    };
    frame = requestAnimationFrame(step);
    return () => cancelAnimationFrame(frame);
  }, [target, ms]);

  return shown;
}
