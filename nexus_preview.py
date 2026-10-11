"""An asynchronous comparison window; no native PDF calls in the UI process."""
from __future__ import annotations

import base64
import io
import queue
import time
from pathlib import Path

import customtkinter as ctk
from PIL import Image
import nexus_workers


class ComparisonWindow(ctk.CTkToplevel):
    def __init__(self, section, ctx, kind, payload):
        super().__init__(section.app)
        self.section, self.ctx, self.kind, self.payload = section, ctx, kind, dict(payload)
        self.messages = queue.Queue()
        self.images = None
        self._closed = False
        self.title(ctx.t("preview_title"))
        self.minsize(820, 560)
        ctx.center_window(self, 1060, 730)
        self.protocol("WM_DELETE_WINDOW", self._close)
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=18, pady=(16, 6))
        ctk.CTkLabel(header, text=Path(payload["input"]).name,
                     font=ctk.CTkFont(size=16, weight="bold")).pack(side="left")
        self.view = ctk.StringVar(value=ctx.t("whole_page"))
        ctk.CTkSegmentedButton(header, variable=self.view,
                               values=[ctx.t("whole_page"), ctx.t("detail_view")],
                               command=lambda _: self._draw()).pack(side="right")
        self.status = ctk.CTkLabel(self, text=ctx.t("preview_loading"),
                                  text_color=ctx.CLR_MUTED, anchor="w")
        self.status.pack(fill="x", padx=18, pady=(0, 8))
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=16, pady=4)
        body.grid_columnconfigure((0, 1), weight=1, uniform="comparison")
        body.grid_rowconfigure(1, weight=1)
        self.labels = []
        for column, key in enumerate(("before", "after")):
            ctk.CTkLabel(body, text=ctx.t(key), font=ctk.CTkFont(weight="bold")).grid(
                row=0, column=column, pady=(0, 5))
            label = ctk.CTkLabel(body, text="…", fg_color=ctx.CLR_LIST_BG, corner_radius=10)
            label.grid(row=1, column=column, sticky="nsew", padx=5)
            self.labels.append(label)
        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.pack(fill="x", padx=18, pady=(8, 14))
        self.note = ctk.CTkLabel(footer, text=ctx.t("preview_sample") if kind == "pdf" else "",
                                text_color=ctx.CLR_MUTED, wraplength=750, justify="left")
        self.note.pack(side="left", fill="x", expand=True)
        self.page = ctk.StringVar(value="1")
        if kind == "pdf":
            ctk.CTkLabel(footer, text=ctx.t("preview_page")).pack(side="left", padx=4)
            self.page_menu = ctk.CTkOptionMenu(footer, variable=self.page, values=["1"], width=90,
                                               command=lambda value: self._load(int(value) - 1))
            self.page_menu.pack(side="left", padx=4)
        self._resize_job = None
        self.bind("<Configure>", self._resize)
        self._load(0)
        self.after(80, self._poll)

    def _load(self, page):
        if getattr(self.section, "_preview_busy", False) or not self.section._can_begin():
            return
        self.section._preview_busy = True
        payload = {**self.payload, "page": page, "op": "preview"}
        self.status.configure(text=self.ctx.t("preview_loading"))
        if self.kind == "pdf":
            self.page_menu.configure(state="disabled")

        def work():
            try:
                if self._closed:
                    return
                if self.kind == "pdf":
                    result = nexus_workers.request_pdf(payload)
                    result["pil"] = []
                    for encoded in result.pop("images"):
                        with Image.open(io.BytesIO(base64.b64decode(encoded))) as image:
                            result["pil"].append(image.copy())
                else:
                    self.ctx.TEMP_DIR.mkdir(parents=True, exist_ok=True)
                    suffix = ".jpg" if payload["format"].upper() in ("JPEG", "JPG") else "." + payload["format"].lower()
                    output = str(self.ctx.TEMP_DIR / f"preview_{time.time_ns()}{suffix}")
                    try:
                        result_tuple = self.ctx.process_image(
                            payload["input"], output, payload["quality"], payload["max_side"],
                            payload["format"], payload["smart"], payload["effort"], payload["target_bytes"])
                        if not result_tuple[1] and result_tuple[2] not in ("err_no_gain", "err_target_unreachable"):
                            raise ValueError(result_tuple[2])
                        pil = [self.ctx.ScanSessionStore.open_image(path, (2600, 2600)).convert("RGB")
                               for path in (payload["input"], output if result_tuple[1] else payload["input"])]
                        result = {"pil": pil, "original_bytes": result_tuple[3], "new_bytes": result_tuple[4],
                                  "status": result_tuple[2]}
                    finally:
                        Path(output).unlink(missing_ok=True)
                self.messages.put(("result", result))
            except Exception as error:
                self.messages.put(("error", str(error) or "stopped"))
            finally:
                self.section._preview_busy = False
        self.section._start_task(work, name="comparison")

    def _poll(self):
        if self._closed:
            return
        try:
            while True:
                kind, result = self.messages.get_nowait()
                if self.kind == "pdf":
                    self.page_menu.configure(state="normal")
                if kind == "error":
                    self.status.configure(text=self.ctx.tr_msg(result), text_color=self.ctx.CLR_ERR)
                    continue
                self.images = result["pil"]
                self.status.configure(
                    text=f"{self.ctx.human_size(result['original_bytes'])} → {self.ctx.human_size(result['new_bytes'])}",
                    text_color=self.ctx.CLR_ACC)
                if self.kind == "pdf":
                    self.page_menu.configure(values=[str(index + 1) for index in range(result["pages"])])
                    self.page.set(str(result["page"] + 1))
                    self.note.configure(text=self.ctx.t("preview_target_sample" if self.payload.get("target_bytes") else "preview_sample"))
                elif result.get("status") in ("OK_TARGET_MISSED", "err_target_unreachable"):
                    self.note.configure(text=self.ctx.t("target_missed"))
                self._draw()
        except queue.Empty:
            pass
        self.after(80, self._poll)

    def _resize(self, event):
        if event.widget is not self or not self.images:
            return
        if self._resize_job:
            self.after_cancel(self._resize_job)
        self._resize_job = self.after(100, self._draw)

    def _draw(self):
        self._resize_job = None
        if self._closed or not self.images:
            return
        width = max(220, int(self.winfo_width() / self._get_window_scaling() / 2) - 28)
        height = max(220, int(self.winfo_height() / self._get_window_scaling()) - 170)
        detail = self.view.get() == self.ctx.t("detail_view")
        reference = self.images[1]
        for image, label in zip(self.images, self.labels):
            if detail:
                crop_width = min(reference.width, width)
                crop_height = min(reference.height, height)
                w = min(image.width, int(crop_width * image.width / reference.width))
                h = min(image.height, int(crop_height * image.height / reference.height))
                left, top = (image.width - w) // 2, (image.height - h) // 2
                view = image.crop((left, top, left + w, top + h))
            else:
                view = image.copy()
            view.thumbnail((width * 2, height * 2), Image.Resampling.LANCZOS)
            ratio = min(width / view.width, height / view.height, 1.0)
            display = (max(1, int(view.width * ratio)), max(1, int(view.height * ratio)))
            rendered = ctk.CTkImage(light_image=view, dark_image=view, size=display)
            label.configure(image=rendered, text="")
            label._comparison_image = rendered

    def _close(self):
        self._closed = True
        self.images = None
        self.destroy()
