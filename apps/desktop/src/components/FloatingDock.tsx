import {
  motion,
  AnimatePresence,
  useMotionValue,
  useSpring,
  useTransform,
  type MotionValue,
} from "framer-motion";
import { useRef, useState, ReactNode } from "react";
import { useMotion } from "../motion";

interface DockItem {
  title: string;
  icon: ReactNode;
  onClick: () => void;
  active?: boolean;
}

export function FloatingDock({
  items,
  className,
}: {
  items: DockItem[];
  className?: string;
}) {
  const mouseX = useMotionValue(Infinity);

  return (
    <motion.div
      onMouseMove={(e) => mouseX.set(e.pageX)}
      onMouseLeave={() => mouseX.set(Infinity)}
      className={`flex items-end gap-2 rounded-2xl px-3 py-2 ${className ?? ""}`}
      style={{
        background: "rgba(15, 14, 12, 0.92)",
        border: "1px solid rgba(255, 255, 255, 0.08)",
      }}
    >
      {items.map((item) => (
        <DockIcon key={item.title} item={item} mouseX={mouseX} />
      ))}
    </motion.div>
  );
}

function DockIcon({ item, mouseX }: { item: DockItem; mouseX: MotionValue<number> }) {
  const ref = useRef<HTMLDivElement>(null);
  const [hovered, setHovered] = useState(false);
  const { reduced } = useMotion();

  const distance = useTransform(mouseX, (val: number) => {
    const bounds = ref.current?.getBoundingClientRect() ?? { x: 0, width: 0 };
    return val - bounds.x - bounds.width / 2;
  });

  // Reduced motion: a fixed-size dock (no cursor magnification).
  const sizeT = useTransform(
    distance,
    [-120, 0, 120],
    reduced ? [42, 42, 42] : [36, 52, 36],
  );
  const size = useSpring(sizeT, { mass: 0.1, stiffness: 150, damping: 12 });

  return (
    <motion.div
      ref={ref}
      style={{ width: size, height: size }}
      onClick={item.onClick}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      className="relative flex items-center justify-center rounded-xl cursor-pointer transition-colors"
    >
      <div
        className="flex items-center justify-center w-full h-full"
        style={{
          color: item.active ? "rgba(232, 226, 216, 0.95)" : "rgba(168, 158, 148, 0.6)",
        }}
      >
        {item.icon}
      </div>

      <AnimatePresence>
        {hovered && (
          <motion.div
            initial={reduced ? { opacity: 0 } : { opacity: 0, y: 6, scale: 0.9 }}
            animate={reduced ? { opacity: 1 } : { opacity: 1, y: 0, scale: 1 }}
            exit={reduced ? { opacity: 0 } : { opacity: 0, y: 6, scale: 0.9 }}
            transition={{ duration: reduced ? 0 : 0.15 }}
            className="absolute -top-9 whitespace-nowrap rounded-md px-2 py-1 text-[10px] font-medium"
            style={{
              background: "rgba(22, 20, 18, 0.95)",
              color: "rgba(232, 226, 216, 0.9)",
              border: "1px solid rgba(255, 255, 255, 0.08)",
            }}
          >
            {item.title}
          </motion.div>
        )}
      </AnimatePresence>
    </motion.div>
  );
}
