import { useState, useEffect, useRef, ReactNode } from "react";

export default function AnimatedPageSwitch({
  active,
  pages,
}: {
  active: string;
  pages: Record<string, ReactNode>;
}) {
  const [current, setCurrent] = useState(active);
  const [animKey, setAnimKey] = useState(0);
  const prevActive = useRef(active);

  useEffect(() => {
    if (active !== prevActive.current) {
      prevActive.current = active;
      setCurrent(active);
      setAnimKey((k) => k + 1);
    }
  }, [active]);

  return (
    <div key={animKey} className="tab-fade">
      {pages[current]}
    </div>
  );
}
