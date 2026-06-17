class PriceBreakdownFields:
    __slots__ = (
        "subtotal_display",
        "fees_tax_display",
        "delivery_fee_display",
        "discounts_display",
        "tip_display",
        "total_display",
        "original_total_display",
    )

    def __init__(
        self,
        *,
        subtotal_display: str,
        fees_tax_display: str,
        delivery_fee_display: str,
        discounts_display: str,
        tip_display: str = "",
        total_display: str,
        original_total_display: str = "",
    ) -> None:
        self.subtotal_display = subtotal_display
        self.fees_tax_display = fees_tax_display
        self.delivery_fee_display = delivery_fee_display
        self.discounts_display = discounts_display
        self.tip_display = tip_display
        self.total_display = total_display
        self.original_total_display = original_total_display
