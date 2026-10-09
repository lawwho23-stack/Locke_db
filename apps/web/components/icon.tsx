import type { CSSProperties } from "react";
export type IconName =
  | "memory"
  | "source"
  | "task"
  | "skill"
  | "activity"
  | "graph"
  | "connection"
  | "usage"
  | "arrow"
  | "refresh"
  | "close"
  | "menu"
  | "logout"
  | "search"
  | "terminal";
const paths: Record<IconName, string> = {
  memory: "M12 3 3 7.5 12 12l9-4.5L12 3ZM3 12l9 4.5 9-4.5M3 16.5l9 4.5 9-4.5",
  source: "M14 2H5v20h14V7l-5-5Zm0 0v6h5M8 12h8M8 16h6",
  task: "M9 6h12M9 12h12M9 18h12M3 5l1 1 2-2M3 11l1 1 2-2M3 17l1 1 2-2",
  skill: "m12 2 8 4v7c0 5-8 9-8 9s-8-4-8-9V6l8-4Zm-4 10 3 3 5-6",
  activity: "M2 12h4l3-8 6 16 3-8h4",
  graph:
    "M6 6h0M18 6h0M12 18h0M8 6h8M7 8l4 8M17 8l-4 8M4 4h4v4H4V4Zm12 0h4v4h-4V4Zm-6 12h4v4h-4v-4Z",
  connection:
    "m9 15 6-6M8 16l-2 2a4 4 0 0 1-6-6l4-4a4 4 0 0 1 6 0m4 0 2-2a4 4 0 0 1 6 6l-4 4a4 4 0 0 1-6 0",
  usage: "M4 21V10h4v11M10 21V4h4v17M16 21V13h4v8",
  arrow: "M4 12h16m-6-6 6 6-6 6",
  refresh: "M20 7a9 9 0 1 0 1 8M20 2v6h-6",
  close: "m6 6 12 12M6 18 18 6",
  menu: "M3 6h18M3 12h18M3 18h18",
  logout: "M9 3H3v18h6M9 12h12m-5-5 5 5-5 5",
  search: "M10 3a7 7 0 1 0 0 14 7 7 0 0 0 0-14Zm5 12 6 6",
  terminal: "m5 6 6 6-6 6M13 18h6",
};
export default function Icon({
  name,
  size = 18,
  style,
}: {
  name: IconName;
  size?: number;
  style?: CSSProperties;
}) {
  return (
    <svg
      aria-hidden="true"
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      style={style}
    >
      <path d={paths[name]} />
    </svg>
  );
}
