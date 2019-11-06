from eth_typing import (
    BLSPubkey,
    BLSSignature,
    Hash32,
)
from eth_utils import (
    big_endian_to_int,
)

from py_ecc.fields import (
    optimized_bls12_381_FQ as FQ,
    optimized_bls12_381_FQ2 as FQ2,
)
from py_ecc.optimized_bls12_381 import (
    Z1,
    Z2,
    b,
    b2,
    field_modulus as q,
    is_inf,
    is_on_curve,
    add,
    normalize,
    optimized_swu_G2,
    multiply_clear_cofactor_G2,
    iso_map_G2,
)

from .constants import (
    POW_2_381,
    POW_2_382,
    POW_2_383,
    FQ2_ORDER,
    EIGTH_ROOTS_OF_UNITY,
    HASH_TO_G2_L,
    DST,
)
from .hash import (
    hkdf_expand,
    hkdf_extract,
)
from .typing import (
    G1Compressed,
    G1Uncompressed,
    G2Compressed,
    G2Uncompressed,
)

#
# Helpers
#


def modular_squareroot_in_FQ2(value: FQ2) -> FQ2:
    """
    ``modular_squareroot_in_FQ2(x)`` returns the value ``y`` such that ``y**2 % q == x``,
    and None if this is not possible. In cases where there are two solutions,
    the value with higher imaginary component is favored;
    if both solutions have equal imaginary component the value with higher real
    component is favored.
    """
    candidate_squareroot = value ** ((FQ2_ORDER + 8) // 16)
    check = candidate_squareroot ** 2 / value
    if check in EIGTH_ROOTS_OF_UNITY[::2]:
        x1 = candidate_squareroot / EIGTH_ROOTS_OF_UNITY[EIGTH_ROOTS_OF_UNITY.index(check) // 2]
        x2 = -x1
        x1_re, x1_im = x1.coeffs
        x2_re, x2_im = x2.coeffs
        return x1 if (x1_im > x2_im or (x1_im == x2_im and x1_re > x2_re)) else x2
    return None


def hash_to_G2(message_hash: Hash32) -> G2Uncompressed:
    u0 = hash_to_base_FQ2(message_hash, 0)
    u1 = hash_to_base_FQ2(message_hash, 1)
    q0 = map_to_curve_G2(u0)
    q1 = map_to_curve_G2(u1)
    r = add(q0, q1)
    p = clear_cofactor_G2(r)
    return p


def hash_to_base_FQ2(message_hash: Hash32, ctr: int) -> FQ2:
    """
    Hash To Base for FQ2

    Converts a message to a point in the finite field as defined here:
    https://tools.ietf.org/html/draft-irtf-cfrg-hash-to-curve-04#section-5
    """
    m_prime = hkdf_extract(DST, message_hash)
    e = []

    for i in range(1, 3):
        # Concatenate ("H2C" || I2OSP(ctr, 1) || I2OSP(i, 1))
        info = b'H2C' + bytes([ctr]) + bytes([i])
        t = hkdf_expand(m_prime, info, HASH_TO_G2_L)
        e.append(big_endian_to_int(t))

    return FQ2(e)


def map_to_curve_G2(u: FQ2) -> G2Uncompressed:
    """
    Map To Curve for G2

    First, convert FQ2 point to a point on the 3-Isogeny curve.
    SWU Map: https://tools.ietf.org/html/draft-irtf-cfrg-hash-to-curve-04#section-6.5.2

    Second, map 3-Isogeny curve to BLS381-G2 curve.
    3-Isogeny Map: https://tools.ietf.org/html/draft-irtf-cfrg-hash-to-curve-04#appendix-C.2
    """
    (x, y, z) = optimized_swu_G2(u)
    return iso_map_G2(x, y, z)


def clear_cofactor_G2(p: G2Uncompressed) -> G2Uncompressed:
    """
    Clear Cofactor via Multiplication

    Ensure a point falls in the correct sub group of the curve.
    Optimization by applying Section 4.1 of https://eprint.iacr.org/2017/419
    However `US patent 7110538` covers the optimization and so it may not
    be able to be applied.
    """
    return multiply_clear_cofactor_G2(p)


#
# G1
#
def compress_G1(pt: G1Uncompressed) -> G1Compressed:
    """
    A compressed point is a 384-bit integer with the bit order (c_flag, b_flag, a_flag, x),
    where the c_flag bit is always set to 1,
    the b_flag bit indicates infinity when set to 1,
    the a_flag bit helps determine the y-coordinate when decompressing,
    and the 381-bit integer x is the x-coordinate of the point.
    """
    if is_inf(pt):
        # Set c_flag = 1 and b_flag = 1. leave a_flag = x = 0
        return G1Compressed(POW_2_383 + POW_2_382)
    else:
        x, y = normalize(pt)
        # Record y's leftmost bit to the a_flag
        a_flag = (y.n * 2) // q
        # Set c_flag = 1 and b_flag = 0
        return G1Compressed(x.n + a_flag * POW_2_381 + POW_2_383)


def decompress_G1(z: G1Compressed) -> G1Uncompressed:
    """
    Recovers x and y coordinates from the compressed point.
    """
    # b_flag == 1 indicates the infinity point
    b_flag = (z % POW_2_383) // POW_2_382
    if b_flag == 1:
        return Z1
    x = z % POW_2_381

    # Try solving y coordinate from the equation Y^2 = X^3 + b
    # using quadratic residue
    y = pow((x**3 + b.n) % q, (q + 1) // 4, q)

    if pow(y, 2, q) != (x**3 + b.n) % q:
        raise ValueError(
            "The given point is not on G1: y**2 = x**3 + b"
        )
    # Choose the y whose leftmost bit is equal to the a_flag
    a_flag = (z % POW_2_382) // POW_2_381
    if (y * 2) // q != a_flag:
        y = q - y
    return (FQ(x), FQ(y), FQ(1))


def G1_to_pubkey(pt: G1Uncompressed) -> BLSPubkey:
    z = compress_G1(pt)
    return BLSPubkey(z.to_bytes(48, "big"))


def pubkey_to_G1(pubkey: BLSPubkey) -> G1Uncompressed:
    z = big_endian_to_int(pubkey)
    return decompress_G1(G1Compressed(z))

#
# G2
#


def compress_G2(pt: G2Uncompressed) -> G2Compressed:
    """
    The compressed point (z1, z2) has the bit order:
    z1: (c_flag1, b_flag1, a_flag1, x1)
    z2: (c_flag2, b_flag2, a_flag2, x2)
    where
    - c_flag1 is always set to 1
    - b_flag1 indicates infinity when set to 1
    - a_flag1 helps determine the y-coordinate when decompressing,
    - a_flag2, b_flag2, and c_flag2 are always set to 0
    """
    if not is_on_curve(pt, b2):
        raise ValueError(
            "The given point is not on the twisted curve over FQ**2"
        )
    if is_inf(pt):
        return G2Compressed((POW_2_383 + POW_2_382, 0))
    x, y = normalize(pt)
    x_re, x_im = x.coeffs
    y_re, y_im = y.coeffs
    # Record the leftmost bit of y_im to the a_flag1
    # If y_im happens to be zero, then use the bit of y_re
    a_flag1 = (y_im * 2) // q if y_im > 0 else (y_re * 2) // q

    # Imaginary part of x goes to z1, real part goes to z2
    # c_flag1 = 1, b_flag1 = 0
    z1 = x_im + a_flag1 * POW_2_381 + POW_2_383
    # a_flag2 = b_flag2 = c_flag2 = 0
    z2 = x_re
    return G2Compressed((z1, z2))


def decompress_G2(p: G2Compressed) -> G2Uncompressed:
    """
    Recovers x and y coordinates from the compressed point (z1, z2).
    """
    z1, z2 = p

    # b_flag == 1 indicates the infinity point
    b_flag1 = (z1 % POW_2_383) // POW_2_382
    if b_flag1 == 1:
        return Z2

    x1 = z1 % POW_2_381
    x2 = z2
    # x1 is the imaginary part, x2 is the real part
    x = FQ2([x2, x1])
    y = modular_squareroot_in_FQ2(x**3 + b2)
    if y is None:
        raise ValueError("Failed to find a modular squareroot")

    # Choose the y whose leftmost bit of the imaginary part is equal to the a_flag1
    # If y_im happens to be zero, then use the bit of y_re
    a_flag1 = (z1 % POW_2_382) // POW_2_381
    y_re, y_im = y.coeffs
    if (y_im > 0 and (y_im * 2) // q != a_flag1) or (y_im == 0 and (y_re * 2) // q != a_flag1):
        y = FQ2((y * -1).coeffs)

    if not is_on_curve((x, y, FQ2([1, 0])), b2):
        raise ValueError(
            "The given point is not on the twisted curve over FQ**2"
        )
    return (x, y, FQ2([1, 0]))


def G2_to_signature(pt: G2Uncompressed) -> BLSSignature:
    z1, z2 = compress_G2(pt)
    return BLSSignature(
        z1.to_bytes(48, "big") + z2.to_bytes(48, "big")
    )


def signature_to_G2(signature: BLSSignature) -> G2Uncompressed:
    p = G2Compressed(
        (big_endian_to_int(signature[:48]), big_endian_to_int(signature[48:]))
    )
    return decompress_G2(p)
