# Printer setup and test

The app sends raster labels directly to Brother QL printers over USB. Choose a supported roll in Admin → Printers and Labels, then use **Test Print** to confirm its orientation and cut position.

On Windows, connect the printer by USB and ensure that P-touch Editor Lite is not holding the device. On macOS and Linux, install `pyusb` and the platform's `libusb` package. If no printer appears, check the USB cable, power and permissions, then restart the app.

A separate numbered print test is available from the command line:

```sh
python run.py print-test --help
```

Use an empty roll or supervise the first hardware test. Confirm that each label is readable and that the cutter works before using it during a service.
