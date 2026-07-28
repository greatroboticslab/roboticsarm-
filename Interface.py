import threading
from time import sleep
import customtkinter
from dobot_util import Dobot

# ---------------------------------------------------------------------------
# Standalone robot initialisation
# ---------------------------------------------------------------------------
# Interface.py manages its own robot connection so that it can be run
# independently without importing main.py.  Importing main.py would execute
# all of its top-level code (creating a second tk.Tk() window, calling
# initialize_robot() a second time, etc.) which breaks both windows.

_robot = None
_robot_connected = False


def initialize_robot(ip: str = "192.168.1.6") -> bool:
    """
    Open a connection to the Dobot at *ip*, clear alarms, enable motors,
    and wait up to 15 s for the robot to reach ENABLE state (mode 5).

    Returns True on success, False if the robot is unreachable (demo mode).
    """
    global _robot, _robot_connected

    try:
        print(f"Connecting to robot at {ip} …")
        _robot = Dobot(ip, logging=True)

        err = _robot.dashboard.clear_error()
        print(f"  ClearError  : {err if err else 'OK'}")
        sleep(0.3)

        err = _robot.dashboard.continue_motion()
        print(f"  Continue    : {err if err else 'OK'}")
        sleep(0.3)

        err = _robot.dashboard.enable()
        print(f"  EnableRobot : {err if err else 'OK'}")

        print("  Waiting for ENABLE state …")
        for attempt in range(30):          # up to 15 s
            sleep(0.5)
            try:
                mode = _robot.dashboard.robot_mode()
                print(f"  Mode check {attempt + 1:02d}: {mode}")
                if mode == 5:
                    break
            except Exception as exc:
                print(f"  Mode check error: {exc}")
        else:
            raise RuntimeError(
                "Robot did not reach ENABLE state within 15 s. "
                "Check for alarms or press the E-stop release."
            )

        sleep(0.5)   # settle before first motion command
        _robot_connected = True
        print("Robot ready.")
        return True

    except Exception as exc:
        print(f"Connection failed: {exc}\nRunning in demo mode.")
        _robot = None
        _robot_connected = False
        return False


# ---------------------------------------------------------------------------
# InfoFrame
# ---------------------------------------------------------------------------

class InfoFrame(customtkinter.CTkFrame):
    def __init__(self, master, point_frame):
        super().__init__(master)
        self.point_frame = point_frame
        self.grid_columnconfigure((0, 1, 2, 3, 4, 5), weight=1)

        # ── Row 0 & 1: Address ─────────────────────────────────────────
        self.title_addr = customtkinter.CTkLabel(
            self, text="Address", fg_color="gray30", corner_radius=6
        )
        self.title_addr.grid(
            row=0, column=0, padx=10, pady=(10, 0), sticky="ew", columnspan=6
        )

        self.ip_label = customtkinter.CTkLabel(self, text="Robot IP:")
        self.ip_label.grid(row=1, column=0, padx=10, pady=5, sticky="ew")

        self.ip_input = customtkinter.CTkEntry(self, placeholder_text="192.168.1.6")
        self.ip_input.grid(row=1, column=1, padx=10, pady=5, sticky="ew", columnspan=2)

        # "Confirm" → connect to the robot at the entered IP
        self.confirm_button = customtkinter.CTkButton(
            self, text="Confirm", command=self.connect
        )
        self.confirm_button.grid(row=1, column=5, padx=10, pady=10, sticky="ew")

        # ── Row 2 & 3: Standard Inputs ─────────────────────────────────
        self.title_inp = customtkinter.CTkLabel(
            self, text="Inputs", fg_color="gray30", corner_radius=6
        )
        self.title_inp.grid(
            row=2, column=0, padx=10, pady=(10, 0), sticky="ew", columnspan=6
        )

        self.x_input = customtkinter.CTkEntry(self, placeholder_text="X")
        self.x_input.grid(row=3, column=0, padx=10, pady=5, sticky="ew")

        self.y_input = customtkinter.CTkEntry(self, placeholder_text="Y")
        self.y_input.grid(row=3, column=1, padx=10, pady=5, sticky="ew")

        self.z_input = customtkinter.CTkEntry(self, placeholder_text="Z")
        self.z_input.grid(row=3, column=2, padx=10, pady=5, sticky="ew")

        self.state_var = customtkinter.StringVar(value="open")
        self.open_radio = customtkinter.CTkRadioButton(
            self, text="Open", variable=self.state_var, value="open"
        )
        self.open_radio.grid(row=3, column=3, padx=10, pady=5, sticky="w")

        self.close_radio = customtkinter.CTkRadioButton(
            self, text="Close", variable=self.state_var, value="close"
        )
        self.close_radio.grid(row=3, column=4, padx=10, pady=5, sticky="w")

        # "Upload" → queue the entered XYZ point for execution
        self.upload_button = customtkinter.CTkButton(
            self, text="Upload", command=self.upload_point
        )
        self.upload_button.grid(row=3, column=5, padx=10, pady=10, sticky="ew")

        # ── Row 4, 5, 6: Manual Controls ───────────────────────────────
        self.title_man = customtkinter.CTkLabel(
            self, text="Manual Controls", fg_color="gray30", corner_radius=6
        )
        self.title_man.grid(
            row=4, column=0, padx=10, pady=(20, 0), sticky="ew", columnspan=6
        )

        self.up_btn = customtkinter.CTkButton(
            self, text="Z Up (W)",
            command=lambda: self.master.manual_z(10)
        )
        self.up_btn.grid(row=5, column=0, padx=5, pady=10, sticky="ew", columnspan=2)

        self.down_btn = customtkinter.CTkButton(
            self, text="Z Down (S)",
            command=lambda: self.master.manual_z(-10)
        )
        self.down_btn.grid(row=5, column=2, padx=5, pady=10, sticky="ew", columnspan=2)

        self.claw_active = False
        self.claw_btn = customtkinter.CTkButton(
            self, text="Claw: OFF", fg_color="darkred",
            command=self.toggle_claw_ui
        )
        self.claw_btn.grid(row=5, column=4, padx=5, pady=10, sticky="ew", columnspan=2)

        self.manual_enabled = customtkinter.BooleanVar(value=False)
        self.safety_switch = customtkinter.CTkSwitch(
            self, text="Enable Keyboard Control", variable=self.manual_enabled
        )
        self.safety_switch.grid(
            row=6, column=0, padx=10, pady=10, sticky="ew", columnspan=6
        )

    # ── Helpers ────────────────────────────────────────────────────────

    def toggle_claw_ui(self):
        self.claw_active = not self.claw_active
        self.claw_btn.configure(
            text="Claw: ON" if self.claw_active else "Claw: OFF",
            fg_color="green" if self.claw_active else "darkred",
        )
        self.master.manual_claw(1 if self.claw_active else 0)

    def connect(self):
        """Connect (or reconnect) to the robot at the IP in the entry box."""
        target_ip = self.ip_input.get().strip() or "192.168.1.6"
        # Run in a background thread so the UI stays responsive while
        # initialize_robot() polls the mode for up to 15 seconds.
        threading.Thread(
            target=self._connect_worker, args=(target_ip,), daemon=True
        ).start()

    def _connect_worker(self, ip: str):
        success = initialize_robot(ip)
        # Schedule the UI update back on the main thread
        self.after(0, lambda: self._on_connect_done(ip, success))

    def _on_connect_done(self, ip: str, success: bool):
        if success:
            self.point_frame.add_command(f"System: {ip} Connected")
        else:
            self.point_frame.add_command("System: Demo Mode (no robot)")

    def upload_point(self):
        """
        Read X, Y, Z and claw state from the input fields and queue the point.
        """
        try:
            x = float(self.x_input.get())
            y = float(self.y_input.get())
            z = float(self.z_input.get())
        except ValueError:
            self.point_frame.add_command("Error: X, Y, Z must be numbers")
            return

        if not (5.0 <= z <= 245.0):
            self.point_frame.add_command("Error: Z must be 5 – 245 mm")
            return

        claw = 1 if self.state_var.get() == "close" else 0
        claw_text = "close" if claw else "open"
        self.point_frame.add_command(
            f"Point: ({x:.1f}, {y:.1f}, z={z:.1f}, claw={claw_text})"
        )

        # If a robot is connected, send the move immediately in a background thread
        if _robot_connected and _robot:
            threading.Thread(
                target=self._execute_point, args=(x, y, z, claw), daemon=True
            ).start()

    def _execute_point(self, x: float, y: float, z: float, claw: int):
        try:
            err = _robot.movement.joint_mov_j([x, y, z, 0.0])
            if err:
                self.after(0, lambda: self.point_frame.add_command(f"Move error: {err}"))
                return
            _robot.movement.sync()
            # Claw: DO1 for open, DO2 for close (immediate dashboard commands)
            if claw:
                _robot.dashboard.set_digital_output(1, 0)
                _robot.dashboard.set_digital_output(2, 1)
            else:
                _robot.dashboard.set_digital_output(1, 1)
                _robot.dashboard.set_digital_output(2, 0)
            self.after(0, lambda: self.point_frame.add_command("Point done"))
        except Exception as exc:
            self.after(0, lambda: self.point_frame.add_command(f"Error: {exc}"))


# ---------------------------------------------------------------------------
# GraphFrame
# ---------------------------------------------------------------------------

class GraphFrame(customtkinter.CTkFrame):
    def __init__(self, master):
        super().__init__(master)
        self.grid_columnconfigure(0, weight=1)

        self.title = customtkinter.CTkLabel(
            self, text="Graph", fg_color="gray30", corner_radius=6
        )
        self.title.grid(row=0, column=0, padx=10, pady=(10, 0), sticky="ew")


# ---------------------------------------------------------------------------
# PointFrame
# ---------------------------------------------------------------------------

class PointFrame(customtkinter.CTkFrame):
    def __init__(self, master):
        super().__init__(master)
        self.grid_columnconfigure(0, weight=1)

        self.title = customtkinter.CTkLabel(
            self, text="Point Location", fg_color="gray30", corner_radius=6
        )
        self.title.grid(row=0, column=0, padx=10, pady=(10, 0), sticky="nsew")

        self.grid_rowconfigure(1, weight=5)
        self.command_listbox = customtkinter.CTkTextbox(self, wrap="none")
        self.command_listbox.insert("end", "No commands yet\n")
        self.command_listbox.configure(state="disabled")
        self.command_listbox.grid(row=1, column=0, padx=10, pady=(10, 0), sticky="nsew")

        self.sendButton = customtkinter.CTkButton(
            self, text="Send", fg_color="gray30", corner_radius=6
        )
        self.sendButton.grid(row=2, column=0, padx=10, pady=(10, 0), sticky="nsew")

        self.clearButton = customtkinter.CTkButton(
            self, text="Clear All", fg_color="red", corner_radius=6,
            command=self.clear_commands
        )
        self.clearButton.grid(row=3, column=0, padx=10, pady=(10, 10), sticky="nsew")

    def add_command(self, text: str):
        self.command_listbox.configure(state="normal")
        current = self.command_listbox.get("1.0", "end").strip()
        if current == "No commands yet":
            self.command_listbox.delete("1.0", "end")
        self.command_listbox.insert("end", text + "\n")
        self.command_listbox.configure(state="disabled")

    def clear_commands(self):
        self.command_listbox.configure(state="normal")
        self.command_listbox.delete("1.0", "end")
        self.command_listbox.insert("end", "No commands yet\n")
        self.command_listbox.configure(state="disabled")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

class App(customtkinter.CTk):
    def __init__(self):
        super().__init__()

        self.title("Robotic Arm manual-input control")
        self.geometry("900x900")

        # Track current Z for the manual Z buttons
        self._current_z: float = 200.0

        # Grid layout
        self.grid_rowconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=5)
        self.grid_columnconfigure(0, weight=5)
        self.grid_columnconfigure(1, weight=1)

        # Right column first so we can pass it to InfoFrame
        self.location_frame = PointFrame(self)
        self.location_frame.grid(
            row=0, column=1, padx=10, pady=10, sticky="nsew", rowspan=2
        )

        # Left column
        self.info_frame = InfoFrame(self, self.location_frame)
        self.info_frame.grid(row=0, column=0, padx=10, pady=10, sticky="nsew")

        self.graph_frame = GraphFrame(self)
        self.graph_frame.grid(row=1, column=0, padx=10, pady=10, sticky="nsew")

    # ── Methods called by InfoFrame buttons ────────────────────────────

    def manual_z(self, delta: float):
        """Move Z up or down by *delta* mm, clamped to [5, 245]."""
        if not self.info_frame.manual_enabled.get():
            return
        self._current_z = max(5.0, min(245.0, self._current_z + delta))
        print(f"Manual Z → {self._current_z:.1f} mm")
        if _robot_connected and _robot:
            threading.Thread(
                target=self._z_move_worker, args=(self._current_z,), daemon=True
            ).start()

    def _z_move_worker(self, z: float):
        try:
            _robot.movement.joint_mov_j([0.0, 0.0, z, 0.0])
            _robot.movement.sync()
        except Exception as exc:
            print(f"[Z MOVE ERROR]: {exc}")

    def manual_claw(self, state: int):
        """
        Fire the claw.  state=1 → close (grip), state=0 → open (release).
        Uses dashboard DOExecute so the output fires immediately, independent
        of the motion queue.
        """
        if not _robot_connected or not _robot:
            print(f"DEMO MODE: claw → {'ON' if state else 'OFF'}")
            return
        threading.Thread(
            target=self._claw_worker, args=(state,), daemon=True
        ).start()

    def _claw_worker(self, state: int):
        try:
            if state:
                _robot.dashboard.set_digital_output(1, 0)
                _robot.dashboard.set_digital_output(2, 1)
            else:
                _robot.dashboard.set_digital_output(1, 1)
                _robot.dashboard.set_digital_output(2, 0)
        except Exception as exc:
            print(f"[CLAW ERROR]: {exc}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app = App()
    app.mainloop()
