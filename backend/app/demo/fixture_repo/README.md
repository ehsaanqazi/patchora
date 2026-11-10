# shop-api (synthetic Patchora demo fixture)

A tiny, deliberately buggy service used by Patchora demo mode, the end-to-end tests and the
evaluation suite. It is not a real application.

Known, intentional defects:

* `shop/pricing.py::average_order_value` divides by zero when there are no orders.
* `shop/users.py::display_name` dereferences a missing profile.
