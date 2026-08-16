import { useEffect, useRef, useState } from "react";
import gsap from "gsap";

const prefersReducedMotion =
  typeof window !== "undefined" &&
  window.matchMedia("(prefers-reduced-motion: reduce)").matches;

export function useAnimatedNumber(value: number | undefined, duration = 0.3) {
  const [display, setDisplay] = useState(value ?? 0);
  const previous = useRef(value ?? 0);

  useEffect(() => {
    if (value === undefined) return;
    if (prefersReducedMotion) {
      setDisplay(value);
      previous.current = value;
      return;
    }
    const proxy = { value: previous.current };
    const tween = gsap.to(proxy, {
      value,
      duration,
      ease: "power2.out",
      onUpdate: () => setDisplay(proxy.value),
      onComplete: () => {
        previous.current = value;
      },
    });
    return () => {
      tween.kill();
      previous.current = value;
    };
  }, [value, duration]);

  return display;
}
