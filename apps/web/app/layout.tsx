import type { Metadata } from "next";
import "./globals.css";
import ConfirmationProvider from "@/components/confirmation";
export const metadata: Metadata = {
  title: "Locke — Agent memory",
  description: "Your shared agent memory workspace",
};
export default function Layout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <ConfirmationProvider>{children}</ConfirmationProvider>
      </body>
    </html>
  );
}
