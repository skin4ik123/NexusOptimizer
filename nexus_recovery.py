"""Review skipped scans without losing faint content or failed raster pages."""
from pathlib import Path

import customtkinter as ctk
from tkinter import filedialog

from nexus_support import copy_atomic


class RecoveryWindow(ctk.CTkToplevel):
    def __init__(self, section, ctx):
        super().__init__(section.app)
        self.section, self.ctx = section, ctx
        self.rows = {}
        self.title(ctx.t("recovery_pages"))
        self.minsize(650, 440)
        ctx.center_window(self, 820, 600)
        ctk.CTkLabel(self, text=ctx.t("recovery_pages"), font=ctk.CTkFont(size=18, weight="bold")).pack(
            anchor="w", padx=18, pady=(16, 6))
        ctk.CTkLabel(self, text=ctx.t("session_scan_hint"), wraplength=750, justify="left",
                     text_color=ctx.CLR_MUTED).pack(anchor="w", padx=18, pady=(0, 8))
        self.area = ctk.CTkScrollableFrame(self, fg_color=ctx.CLR_LIST_BG)
        self.area.pack(fill="both", expand=True, padx=16, pady=6)
        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.pack(fill="x", padx=16, pady=(4, 14))
        self.restore_button = ctx.btn_primary(footer, text=ctx.t("restore_selected"), command=self._restore)
        self.restore_button.pack(side="left", padx=4)
        self.save_button = ctx.btn_ghost(footer, text=ctx.t("save_raw"), command=self._save)
        self.save_button.pack(side="left", padx=6)
        self.refresh()

    def refresh(self):
        for widget in self.area.winfo_children():
            widget.destroy()
        self.rows.clear()
        records = self.ctx.ScanSessionStore.recovery()
        records += getattr(self.section, "_transient_recovery", [])
        seen = set()
        for index, record in enumerate(records, 1):
            path = record["path"]
            if path in seen or not Path(path).is_file():
                continue
            seen.add(path)
            row = ctk.CTkFrame(self.area, corner_radius=9)
            row.pack(fill="x", padx=4, pady=4)
            thumb = ctk.CTkLabel(row, text="…", width=72, height=88, fg_color=self.ctx.CLR_GHOST)
            thumb.pack(side="left", padx=8, pady=6)
            selected = ctk.BooleanVar(value=True)
            reason = self.ctx.t("recovery_" + record.get("reason", "error"))
            label = self.ctx.t("scan_page_n").format(n=index) + " · " + reason
            ctk.CTkCheckBox(row, text=label, variable=selected, fg_color=self.ctx.CLR_ACC).pack(
                side="left", padx=10)
            self.rows[path] = {"selected": selected, "thumb": thumb}
            self.section._thumb_request(path, (72, 88), ("recovery", path))
        if not seen:
            ctk.CTkLabel(self.area, text=self.ctx.t("recovery_empty")).pack(pady=30)

    def _selected(self):
        return [path for path, row in self.rows.items() if row["selected"].get()]

    def _restore(self):
        paths = self._selected()
        if not paths:
            return
        if self.section.is_running:
            self.section.show_toast(self.ctx.t("other_task_running"), "warn")
            return
        self.restore_button.configure(state="disabled")
        self.section._recovery_busy = True
        def work():
            restored = self.ctx.ScanSessionStore.restore(paths)
            self.section.msg_queue.put(("scan_recovered", restored))
        self.section._start_task(work, name="restore-scans")

    def _save(self):
        paths = self._selected()
        if not paths:
            return
        folder = filedialog.askdirectory(parent=self, title=self.ctx.t("save_raw"))
        if not folder:
            return
        self.save_button.configure(state="disabled")
        self.section._recovery_busy = True
        def work():
            count = 0
            for index, path in enumerate(paths, 1):
                try:
                    output = self.ctx.unique_out_path(str(Path(folder) / f"scan_original_{index:03d}{Path(path).suffix}"))
                    copy_atomic(path, output, self.ctx.TEMP_DIR, self.ctx.stop_flag)
                    count += 1
                except Exception as error:
                    self.ctx._log_exc("save received scan", error)
            self.section.msg_queue.put(("recovery_saved", count, folder))
        self.section._start_task(work, name="save-received-scans")

    def apply_thumb(self, path, pil, display):
        row = self.rows.get(path)
        if row:
            rendered = ctk.CTkImage(light_image=pil, dark_image=pil, size=display)
            row["thumb"].configure(image=rendered, text="")
            row["image"] = rendered
