import { useEffect, useRef, type ReactNode } from "react";
import gsap from "gsap";

export default function Reveal({ children }: { children: ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const node = ref.current;
    if (!node) return;
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (reduce) return;
    const items = Array.from(
      node.querySelectorAll(".metric, .panel, .quota, .section-title"),
    );
    if (items.length === 0) return;
    const ctx = gsap.context(() => {
      gsap.from(items, {
        autoAlpha: 0,
        y: 12,
        duration: 0.3,
        stagger: 0.05,
        ease: "power2.out",
        clearProps: "all",
      });
    }, node);
    return () => ctx.revert();
  }, []);

  return <div ref={ref}>{children}</div>;
}
