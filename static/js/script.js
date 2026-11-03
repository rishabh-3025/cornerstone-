document.addEventListener('DOMContentLoaded', function () {
    const flashCards = document.querySelectorAll('.flash');
    flashCards.forEach((card) => {
        setTimeout(() => {
            card.style.opacity = '0';
            card.style.transform = 'translateY(-8px)';
            card.style.transition = 'all 0.35s ease';
            setTimeout(() => card.remove(), 350);
        }, 3500);
    });

    const paymentSelect = document.querySelector('select[name="payment_method"]');
    const upiPanel = document.getElementById('upi-qr-panel');
    const paymentProofInput = document.getElementById('payment-proof');

    function updatePaymentMethodUI() {
        if (!paymentSelect || !upiPanel) {
            return;
        }

        const isUpi = paymentSelect.value === 'UPI';
        upiPanel.classList.toggle('hidden', !isUpi);
        if (paymentProofInput) {
            paymentProofInput.required = isUpi;
        }
    }

    if (paymentSelect && upiPanel) {
        updatePaymentMethodUI();
        paymentSelect.addEventListener('change', updatePaymentMethodUI);
    }
});
